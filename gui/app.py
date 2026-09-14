#!/usr/bin/env python3
"""
Stock screen  --  local application
===================================

Starts a small web server on your own machine and opens the dashboard in
your browser. Nothing is published and nothing leaves the computer: the
browser is only the display surface, the way a desktop window would be.

    python app.py
    python app.py --no-browser --port 8800

WHY A LOCAL SERVER RATHER THAN A DESKTOP WINDOW
-----------------------------------------------
Two of the requirements point the same way. Pages must be reachable
without switching windows, which routes give for free. And the dashboard
has to open immediately and show a scan in progress rather than waiting
for it to finish, which needs a server that can answer while work
continues.

A Tkinter or Qt window would need an embedded browser to display the
report anyway, so the standard-library server is not an extra dependency
for what the app already needs.

NO WEB FRAMEWORK
----------------
Built on the standard library. Flask would pull in Werkzeug, Jinja2,
click, itsdangerous and MarkupSafe -- five packages, every one of which
has to be bundled into the executable and kept working. For a single-user
application on loopback the standard library is enough, and the installer
stays small and predictable.

SECURITY
--------
Binds to 127.0.0.1 only, so nothing on the network can reach it. Requests
that change state or read settings are checked against the Host header:
any page on the internet can send your browser to http://localhost:8765,
so without that check a website you happened to visit could start a scan
or read back your configuration. API keys are never returned by any route
-- only whether one is set, and where it came from.
"""

from __future__ import annotations

# Run as a script: put the pipeline package (../core) on the import path.
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "core"))

import argparse
import json
import logging
import os
import socket
import threading
import time
import webbrowser
from datetime import date
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import config
import credentials
from tasks import TaskRunner

log = logging.getLogger("app")

APP_DIR = Path(__file__).parent
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


# ---------------------------------------------------------------------------
# What the app knows about itself
# ---------------------------------------------------------------------------


def _excel_installed() -> bool:
    """
    True only if Excel itself is registered for COM -- not just that
    pywin32 is importable.

    pywin32 is bundled into the frozen .exe (see packaging/app.spec), so
    find_spec("win32com") succeeds on every machine the app runs on,
    whether or not Excel is actually installed there. That let this
    check report "Ready" on a computer with no Excel at all: the job
    started, ran every earlier stage, and only discovered there was
    nothing to recalculate with once it reached the first company.
    Checking the Excel.Application ProgID is the same lookup pywin32
    itself has to make inside DispatchEx() -- so failing here means
    DispatchEx() would fail too, without spending the time to find that
    out mid-run and without opening Excel just to ask.
    """
    import importlib.util
    if importlib.util.find_spec("win32com") is None:
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Excel.Application\CLSID")
        return True
    except OSError:
        return False


def recalc_engine() -> dict:
    """
    Which spreadsheet engine is available, if any.

    The valuation is an Excel workbook, so something has to recalculate
    it. Neither Excel nor LibreOffice can be bundled into an executable,
    which is why this is a stated requirement rather than a hidden one --
    and why starting a job without one refuses cleanly, with a plain
    message, instead of failing in the middle of a scan.
    """
    from write_workbook import find_soffice
    found = []
    if _excel_installed():
        found.append("Excel")
    if find_soffice():
        found.append("LibreOffice")
    return {
        "available": found,
        "ok": bool(found),
        "message": ("Ready." if found else
                    "No spreadsheet engine found. Install LibreOffice "
                    "(free) or Microsoft Excel — the valuation is a "
                    "spreadsheet and something has to calculate it."),
    }


def data_status(db: Path, out_dir: Path) -> dict:
    """A plain-language summary of what the app currently holds."""
    runs = sorted(d.name for d in out_dir.glob("*") if d.is_dir())
    status = {
        "database": db.exists(),
        "database_size_mb": round(db.stat().st_size / 1e6, 1) if db.exists() else 0,
        "companies": 0,
        "last_run": runs[-1] if runs else None,
        "runs": len(runs),
        "fiscal_years": config.FISCAL_YEARS,
    }
    if db.exists():
        try:
            from cache import Cache
            with Cache(db) as c:
                status["companies"] = len(c.complete_entities(config.FISCAL_YEARS))
        except Exception as e:                       # noqa: BLE001
            log.warning("could not read the database: %s", e)
            status["error"] = str(e)
    return status


# ---------------------------------------------------------------------------
# Jobs the Settings page can start
# ---------------------------------------------------------------------------


def job_screen(runner: TaskRunner, *, db: Path, out_dir: Path,
               engine: str) -> dict:
    """Value every ready company, then build the report page."""
    import build_report
    from cache import Cache
    from pipeline import eligible_universe, screen_universe

    missing = [p.name for p in (config.PARAMS_PATH, config.TABLES_PATH,
                                config.POLICY_PATH, config.TEMPLATE_PATH,
                                db) if not p.exists()]
    if missing:
        raise RuntimeError(
            "Cannot value companies -- these files are missing from the "
            f"project folder: {', '.join(missing)}. Run the earlier "
            "pipeline steps first.")

    params = json.loads(config.PARAMS_PATH.read_text(encoding="utf-8"))
    tables = json.loads(config.TABLES_PATH.read_text(encoding="utf-8"))
    policy = json.loads(config.POLICY_PATH.read_text(encoding="utf-8"))
    stamp = date.today().isoformat()
    run_dir = out_dir / stamp

    with Cache(db) as cache:
        ready = eligible_universe(cache, params, config.FISCAL_YEARS)
        if not ready:
            raise RuntimeError(
                "Nothing is ready to value. The database has no company "
                "with five complete years, a price and a share count.")
        runner.progress(0, len(ready), "starting")

        def on_progress(done, total, result, lei):
            # Cancellation is checked here rather than inside the pipeline
            # so a stopped run leaves every workbook it already finished
            # intact, instead of a half-written one.
            if runner.cancelled:
                raise KeyboardInterrupt("stopped")
            label = result.ticker if result is not None else "skipped"
            runner.progress(done, total, label)
            # screen_universe() itself prints nothing (pipeline.py's own
            # design -- a silent library call), so without this the
            # activity panel showed the sliding bar and nothing else for
            # the whole run. say() is what actually reaches the panel;
            # progress() alone only drives the percentage and the label
            # next to it.
            runner.say(f"[{done:>4}/{total}] {label}"
                      + (f" — {result.verdict}" if result is not None else ""))

        try:
            summary = screen_universe(
                cache, params, tables, policy, config.TEMPLATE_PATH,
                run_dir, config.FISCAL_YEARS, engine=engine,
                param_paths=(config.PARAMS_PATH, config.TABLES_PATH),
                on_progress=on_progress)
        except KeyboardInterrupt:
            return {"cancelled": True, "run_dir": str(run_dir)}

    from run_screen import write_results_workbook
    summary.results_path = run_dir / f"results_{stamp}.xlsx"
    write_results_workbook(summary.results_path, summary)
    summary.run_json_path = run_dir / f"run_{stamp}.json"
    summary.run_json_path.write_text(
        json.dumps(summary.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8")

    runner.progress(len(summary.companies), len(summary.companies),
                    "writing the report")
    report = run_dir / f"report_{stamp}.html"
    report.write_text(
        build_report.render(build_report.load(run_dir)),
        encoding="utf-8")

    return {
        "run_dir": str(run_dir), "report": str(report),
        "valued": len(summary.companies),
        "queue": [c.ticker for c in summary.research_queue],
        "undervalued": len(summary.undervalued),
        "skipped": len(summary.skipped),
    }


# The ingestion stages are still scripts with a main(). Rather than
# rewrite six of them before Settings can drive them, each is invoked
# in-process with the argv it expects. Output is captured by the runner and
# appears in the activity panel, so the interface shows the same detail the
# terminal did.
#
# In-process rather than subprocess on purpose: a subprocess would not
# inherit the captured stdout, so the panel would sit empty for minutes
# while the work happened somewhere the user could not see.
SCRIPT_JOBS = {
    "coverage":   ("esef_coverage",      ["--refresh"]),
    "extract":    ("esef_extract",       []),
    "identity":   ("resolve_identity",   []),
    # NB: "shares" is NOT here -- shares_worksheet --export only writes a CSV
    # for a human to fill and never touches the share_count table, so the
    # stage stayed "never done" after the button. job_shares() below does
    # the part that can be automated.
    # The full annual parameter refresh: re-download Damodaran's files, take
    # the ECB risk-free rate, then parse country risk (B49/B53/B54) and the
    # industry table (B57:B60) into parameters.json. Previously this button
    # ran --riskfree alone, so the Damodaran matrix could only be refreshed
    # from a terminal.
    "parameters": ("fetch_parameters",
                   ["--inspect", "--refresh", "--riskfree", "--growth",
                    "--countries", "--industries"]),
}

# Which of SCRIPT_JOBS' modules take --db, so job_script() knows whether
# to pass one -- a script that does not take it would abort on an
# argparse error instead of doing the work. Declared here rather than
# read off the module at runtime (inspect.getsource() needs a real .py
# file on disk; a frozen build loads every module from a bundled archive
# instead, and getsource() there raises OSError, not "no --db here").
TAKES_DB = {"esef_extract", "resolve_identity"}


def job_script(runner: TaskRunner, *, job: str, db: Path) -> dict:
    """Run one ingestion stage and surface whatever it printed."""
    import importlib
    import sys

    module_name, extra = SCRIPT_JOBS[job]
    # No count to report -- the ingestion scripts do not expose one -- but a
    # label and a non-zero total keep the dashboard from looking frozen.
    runner.progress(0, 0, f"running {module_name}")
    module = importlib.import_module(module_name)
    entry = getattr(module, "main", None)
    if entry is None:
        raise RuntimeError(
            f"{module_name} has no main() to call. Its command-line entry "
            f"point needs to be exposed as main().")
    argv = sys.argv
    try:
        sys.argv = [f"{module_name}.py", *extra]
        if module_name in TAKES_DB:
            sys.argv += ["--db", str(db)]
        code = entry()
    finally:
        sys.argv = argv
    if code:
        raise RuntimeError(
            f"{module_name} stopped with code {code}. The activity log "
            f"above says why.")
    return {"job": job}


def job_shares(runner: TaskRunner, *, db: Path) -> dict:
    """
    Populate what can be populated for the share-count stage, then refresh
    the hand-entry worksheet.

    In order: import anything already filled into shares.csv, store the
    EPS-derived counts that pass their own cross-check, then re-write
    shares.csv so it shows only the rows that still need a number. Running
    it again after filling those blanks imports them too.
    """
    from cache import Cache
    from shares_worksheet import do_export, do_import, gather, seed_derived

    runner.progress(0, 0, "updating share counts")
    csv_path = config.ROOT / "shares.csv"
    years = config.FISCAL_YEARS
    with Cache(db) as cache:
        imported = do_import(csv_path, cache, years) if csv_path.exists() else 0
        seeded = seed_derived(cache, years)
        rows = gather(cache, years)
        do_export(csv_path, rows)

    blank = sum(1 for r in rows if not r["shares"])
    return {"imported": imported, "seeded": seeded,
            "companies": len(rows), "need_a_number": blank,
            "worksheet": str(csv_path)}


def job_prices(runner: TaskRunner, *, db: Path, source: str) -> dict:
    """Refresh prices for every company with a primary listing."""
    import sys
    import fetch_prices

    def report(done, total, label):
        if runner.cancelled:
            raise KeyboardInterrupt("stopped")
        runner.progress(done, total, label)

    argv = sys.argv
    try:
        sys.argv = ["fetch_prices.py", "--source", source,
                    "--db", str(db), "--refresh"]
        code = fetch_prices.main(on_progress=report)
    except KeyboardInterrupt:
        return {"cancelled": True}
    finally:
        sys.argv = argv
    if code:
        raise RuntimeError(
            "The price fetch did not finish. The activity log above says "
            "why — usually a missing API key or an unreachable source.")
    return {"source": source}


def job_all(runner: TaskRunner, *, db: Path, out_dir: Path,
           engine: str) -> dict:
    """
    "Run everything": every fully-automatable stage, in dependency order.

    Industries needs a person to paste an LLM's reply back, and the
    credit ladder is copied by hand from Damodaran's table each January
    -- neither can run unattended, so neither is here; the Dashboard
    still flags them on their own if they need attention.

    Stops at the first stage that fails or is cancelled, leaving every
    stage before it finished -- nothing after a broken stage would be
    built on good data anyway. Each stage's own print()s and progress()
    calls flow straight into the same activity log and bar this call
    itself is wrapped in, so this reads as one continuous run rather than
    seven separate ones; the "=== step ===" lines just mark where one
    stage ends and the next begins.
    """
    steps = [
        ("coverage", lambda: job_script(runner, job="coverage", db=db)),
        ("extract", lambda: job_script(runner, job="extract", db=db)),
        ("identity", lambda: job_script(runner, job="identity", db=db)),
        ("prices", lambda: job_prices(runner, db=db, source="yahoo")),
        ("shares", lambda: job_shares(runner, db=db)),
        ("parameters", lambda: job_script(runner, job="parameters", db=db)),
        ("screen", lambda: job_screen(runner, db=db, out_dir=out_dir,
                                      engine=engine)),
    ]
    results = {}
    for i, (name, run_step) in enumerate(steps, 1):
        if runner.cancelled:
            return {"cancelled": True, "completed": list(results)}
        print(f"\n=== step {i} of {len(steps)}: {name} ===")
        result = run_step()
        # job_prices() and job_screen() catch their own KeyboardInterrupt
        # and return {"cancelled": True} rather than raising it, so a stop
        # mid-stage has to be checked for here too, not just at the top of
        # the loop.
        if isinstance(result, dict) and result.get("cancelled"):
            return {"cancelled": True, "completed": list(results)}
        results[name] = result
    return {"completed": list(results), "results": results}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "StockScreen"

    def __init__(self, *a, app=None, **kw):
        self.app = app
        super().__init__(*a, **kw)

    # -- plumbing -------------------------------------------------------

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _host_is_local(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        origin = self.headers.get("Origin")
        if host not in ALLOWED_HOSTS:
            return False
        if origin:
            name = urlparse(origin).hostname or ""
            if name not in ALLOWED_HOSTS:
                return False
        return True

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                      # the tab was closed mid-response

    def json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def page(self, html: str, code: int = 200) -> None:
        self._send(code, html.encode("utf-8"), "text/html; charset=utf-8")

    def body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            # rfile.read(n) means "read to EOF" for a negative n, which
            # hangs the request thread indefinitely on a keep-alive
            # connection that never closes -- reject it like any other
            # malformed length instead.
            if n < 0:
                raise ValueError("negative Content-Length")
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    # -- routes ---------------------------------------------------------

    def do_GET(self):
        route = urlparse(self.path).path.rstrip("/") or "/"
        try:
            return self._get(route)
        except Exception as e:                     # noqa: BLE001
            return self._server_error(route, e)

    def _get(self, route: str) -> None:
        app = self.app

        # Any of these means a window is open and watching.
        if route in ("/api/ping", "/api/status", "/api/task"):
            app.touch()
        if route == "/api/ping":
            return self.json({"ok": True})

        if route == "/":
            return self.page(app.render_dashboard())
        if route == "/api/status":
            return self.json(app.status())
        if route == "/api/task":
            return self.json(app.runner.snapshot())
        if route == "/api/industry-prompt":
            return self.json(app.industry_prompt())
        if route == "/api/tables-check":
            return self.json(app.tables_check())
        if route == "/results":
            page = app.render_results()
            if page is None:
                return self.page(app.render_missing(
                    "No results yet",
                    "Run a scan from Settings and the report will appear "
                    "here."))
            return self.page(page)
        if route == "/portfolio":
            return self.page(app.render_portfolio())
        if route == "/settings":
            return self.page(app.render_settings())
        return self.page(app.render_missing(
            "Page not found", "That address does not exist."), 404)

    def do_POST(self):
        route = urlparse(self.path).path.rstrip("/") or "/"
        try:
            return self._post(route)
        except Exception as e:                      # noqa: BLE001
            return self._server_error(route, e)

    def _post(self, route: str) -> None:
        if not self._host_is_local():
            # A website you visit can point your browser at localhost.
            # Without this, it could start a scan or read configuration.
            return self.json({"error": "requests must come from this "
                                       "computer"}, 403)
        app, data = self.app, self.body()

        if route == "/api/run":
            return self.json(*app.start_job(data.get("job", ""), data))
        if route == "/api/cancel":
            return self.json(app.cancel())
        if route == "/api/key":
            return self.json(*app.save_key(data))
        if route == "/api/industry-map":
            return self.json(*app.apply_industry_map(data.get("text", "")))
        if route == "/api/tables-apply":
            return self.json(*app.tables_apply())
        return self.json({"error": "unknown request"}, 404)

    def _server_error(self, route: str, e: Exception) -> None:
        # The Zero Terminal Policy means an unhandled exception in a route
        # handler (a config file missing or mid-write, say) must end up on
        # screen, not as a traceback socketserver prints to real stderr and
        # a dropped connection -- do_GET/do_POST route everything through
        # here instead of letting one escape.
        log.exception("unhandled error handling %s", route)
        if route.startswith("/api/"):
            return self.json({"error": f"Something went wrong: {e}"}, 500)
        return self.page(self.app.render_missing(
            "Something went wrong",
            f"An unexpected error occurred: {e}"), 500)


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------


class App:
    def __init__(self, db: Path, out_dir: Path, engine: str,
                 autostop_grace: float = 10.0, exit_on_close: bool = True,
                 daily_prices: bool = True):
        self.db, self.out_dir, self.engine = db, out_dir, engine
        self.runner = TaskRunner()
        # Once per calendar day, while the app is up, pull fresh prices for
        # the companies currently held in the portfolio (only those -- it is
        # a handful, so it is quick and stays well inside the Yahoo budget).
        self.daily_prices = daily_prices
        # Dead-man switch: every page pings /api/ping every few seconds. If
        # nothing has pinged for `autostop_grace` seconds while a job runs,
        # the window is gone -- close, crash, sleep, network drop -- and the
        # job is stopped so a closed tab never leaves a scrape running. 0
        # disables all window-watching (autostop and exit-on-close both).
        self.autostop_grace = autostop_grace
        # ...and, a bit later, stop the server itself, so closing the
        # browser also frees the terminal. Needs a first ping before it
        # arms (a --no-browser session that no page ever loads is left
        # alone), and a longer grace than a job cancel so a reload or a
        # brief tab switch does not kill it.
        self.exit_on_close = exit_on_close and autostop_grace > 0
        self.shutdown_grace = max(autostop_grace + 5.0, 15.0)
        self.on_close = None            # set by main() to server.shutdown
        self._seen_ping = False
        self._closing = False
        # The last tables_check() result, so tables_apply() writes only
        # what was actually shown to the user -- see tables_check().
        self._tables_fetch: dict | None = None
        self.last_seen = time.monotonic()

    def touch(self) -> None:
        self.last_seen = time.monotonic()
        self._seen_ping = True

    def autostop_tick(self) -> None:
        """On a timer: stop the job, then the server, once the window is gone."""
        if not self.autostop_grace:
            return
        idle = time.monotonic() - self.last_seen

        if self.runner.busy and idle > self.autostop_grace:
            log.info("no window heartbeat for %.0fs - stopping %r",
                     idle, self.runner.snapshot().get("name"))
            self.runner.cancel("stopped - the window was closed")

        if (self.exit_on_close and self._seen_ping and not self._closing
                and self.on_close and idle > self.shutdown_grace):
            self._closing = True
            log.info("no window heartbeat for %.0fs - shutting down", idle)
            self.on_close()

    def daily_price_tick(self) -> None:
        """
        Once per calendar day: pull the held positions' closing prices and
        backfill every day from the earliest gap, so the performance chart
        has the whole time each name has been held even if the app was not
        running to catch it.
        """
        if not self.daily_prices or self.runner.busy:
            return
        from cache import Cache
        from portfolio import Portfolio

        today = date.today().isoformat()
        with Portfolio(config.PORTFOLIO_PATH) as pf, Cache(self.db) as db:
            if pf.prices_refreshed == today:
                return
            opens = pf.open_positions()
            if not opens:
                return
            held = [r["lei"] for r in opens]
            # start the backfill at the oldest missing day across the book:
            # for each name, the day after its latest stored price, or its
            # entry date if it has none.
            since = min(
                max(r["entry_date"],
                    (db.latest_price(r["lei"]) or {}).get("as_of")
                    or r["entry_date"])
                for r in opens)
        if self.runner.busy:                      # a job started meanwhile
            return
        log.info("daily price refresh: %d held, backfill since %s",
                 len(held), since)
        if self._fetch_prices(held, since=since):
            with Portfolio(config.PORTFOLIO_PATH) as pf:
                pf.set_prices_refreshed(today)

    def _fetch_prices(self, leis: list[str], since: str | None = None) -> bool:
        """Run fetch_prices for exactly `leis`. Output goes to the log, not
        the activity panel -- this is background housekeeping."""
        import sys
        import threading
        from contextlib import redirect_stderr, redirect_stdout
        import fetch_prices
        from tasks import _Capture

        # sys.stdout/sys.stderr are process-global, so a bare
        # redirect_stdout() here would race a real job's own redirect in
        # tasks.py's TaskRunner -- whichever exits its `with` block second
        # restores the stream to a now-stale capture object, permanently
        # misrouting output for the rest of the process. stdio_lock is the
        # one thing both sides coordinate through; skip this tick rather
        # than fetch prices with an ill-gotten lock if a job is mid-run.
        if not self.runner.stdio_lock.acquire(blocking=False):
            log.info("daily price refresh skipped: a job is running")
            return False

        extra = ["--backfill-since", since] if since else []
        lines: list[str] = []
        argv = sys.argv
        try:
            sys.argv = ["fetch_prices.py", "--source", "yahoo", "--refresh",
                        "--db", str(self.db), *extra, "--leis", *leis]
            capture = _Capture(lines.append, threading.get_ident(),
                               sys.__stdout__)
            with redirect_stdout(capture), redirect_stderr(capture):
                code = fetch_prices.main()
        except SystemExit as e:
            code = e.code or 0
        except Exception:                          # noqa: BLE001
            log.exception("daily price refresh failed")
            return False
        finally:
            sys.argv = argv
            self.runner.stdio_lock.release()
        tail = " | ".join(lines[-8:])
        log.info("daily price refresh done (exit %s): %s", code, tail[:300])
        return not code

    # -- state ----------------------------------------------------------

    def status(self) -> dict:
        return {
            "engine": recalc_engine(),
            "data": data_status(self.db, self.out_dir),
            "keys": {name: {"label": meta["label"],
                            "where": credentials.describe(name),
                            "set": credentials.is_set(name),
                            "note": meta["note"], "signup": meta["signup"]}
                     for name, meta in credentials.PROVIDERS.items()},
            "keyring": credentials.keyring_available(),
            "task": self.runner.snapshot(),
            "report": bool(self.latest_run_dir()),
        }

    def latest_run_dir(self) -> Path | None:
        """The newest run folder that has provenance sidecars to render."""
        runs = sorted((d for d in self.out_dir.glob("*") if d.is_dir()),
                      reverse=True)
        for run in runs:
            if any(run.glob("*.provenance.json")):
                return run
        return None

    def render_results(self) -> str | None:
        """
        Rebuild the results page from the latest run's sidecars on every
        view, so it always carries the current header, nav and theme --
        the saved report_*.html file can be from an older layout.
        """
        import build_report
        import ui
        run_dir = self.latest_run_dir()
        if run_dir is None:
            return None
        return ui.results_page(
            build_report.render(build_report.load(run_dir)))

    def render_portfolio(self) -> str:
        """
        Apply the latest screening run to the portfolio (open the new
        undervalued names, close the ones whose verdict changed), mark it
        to the current prices, and render it in the results-page style.
        Syncing on view keeps the page in step with the screen; it is
        idempotent, so a repeat view changes nothing.
        """
        import build_report
        import ui
        # First view of the day backfills the held-book price history, so
        # the chart is current when it is looked at (no-ops if already done
        # today or a job is running).
        self.daily_price_tick()
        return ui.results_page(
            build_report.render_portfolio(self._marked_portfolio()),
            current="/portfolio")

    def _marked_portfolio(self) -> dict:
        """Sync the portfolio to the latest run and mark it."""
        from cache import Cache
        from portfolio import Portfolio, mark, sync
        from track import RunFromJson, latest_run

        with Portfolio(config.PORTFOLIO_PATH) as pf, Cache(self.db) as db:
            run_path = latest_run(self.out_dir)
            if run_path is not None and not self.runner.busy:
                try:
                    doc = json.loads(run_path.read_text(encoding="utf-8"))
                    sync(pf, RunFromJson(doc, db, config.FISCAL_YEARS))
                except (OSError, ValueError, KeyError) as e:
                    log.warning("portfolio sync skipped: %s", e)
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

    # -- actions --------------------------------------------------------

    def start_job(self, job: str, data: dict) -> tuple[dict, int]:
        if self.runner.busy:
            return {"error": "Something is already running. Wait for it to "
                             "finish, or stop it first."}, 409

        if job == "screen":
            engine = recalc_engine()
            if not engine["ok"]:
                return {"error": engine["message"]}, 400
            started = self.runner.start("screen", partial(
                job_screen, db=self.db, out_dir=self.out_dir,
                engine=self.engine))
        elif job == "all":
            # Checked up front rather than discovered after six other
            # stages have already run: screen is the last of the seven,
            # and there is no point spending several minutes on the
            # ingestion stages only to fail at the very end for a reason
            # that was already knowable before any of them started.
            engine = recalc_engine()
            if not engine["ok"]:
                return {"error": engine["message"]}, 400
            started = self.runner.start("all", partial(
                job_all, db=self.db, out_dir=self.out_dir,
                engine=self.engine))
        elif job == "prices":
            started = self.runner.start("prices", partial(
                job_prices, db=self.db, source="yahoo"))
        elif job == "shares":
            started = self.runner.start("shares", partial(
                job_shares, db=self.db))
        elif job == "industries":
            return {"error": "Use the industry mapping panel on the "
                             "Settings page: it copies a prompt for a "
                             "language model, and you paste the reply "
                             "back."}, 400
        elif job in SCRIPT_JOBS:
            started = self.runner.start(job, partial(
                job_script, job=job, db=self.db))
        else:
            return {"error": f"Unknown job {job!r}"}, 400

        return ({"started": True} if started
                else {"error": "Could not start"}), (200 if started else 409)

    def cancel(self) -> dict:
        # Every job is stoppable now: screen and prices check cancelled()
        # between items, and the ingestion scripts are interrupted at their
        # next print() by the output capture (see tasks._Capture). Work
        # already finished is left intact; the current item is dropped.
        return {"cancelled": self.runner.cancel()}

    def save_key(self, data: dict) -> tuple[dict, int]:
        provider = data.get("provider")
        key = (data.get("key") or "").strip()
        if provider not in credentials.PROVIDERS:
            return {"error": "Unknown provider"}, 400
        if not key:
            removed = credentials.forget(provider)
            return {"saved": False, "removed": removed}, 200
        try:
            credentials.store(provider, key)
        except credentials.KeyringUnavailable:
            meta = credentials.PROVIDERS[provider]
            return {"error": "This computer has no credential store. Set "
                             f"{meta['env']} in the environment instead."}, 400
        except ValueError as e:
            return {"error": str(e)}, 400
        return {"saved": True, "where": credentials.describe(provider)}, 200

    # -- pages ----------------------------------------------------------

    def render_dashboard(self) -> str:
        import pipeline_status
        import ui
        steps = pipeline_status.stages(db=self.db, out_dir=self.out_dir)
        return ui.dashboard(steps, pipeline_status.alerts(steps),
                            task=self.runner.snapshot())

    def render_settings(self) -> str:
        import pipeline_status
        import ui
        steps = pipeline_status.stages(db=self.db, out_dir=self.out_dir)
        status = self.status()
        return ui.settings(steps, status["keys"], status["engine"])

    def render_missing(self, title: str, message: str) -> str:
        import ui
        return ui.placeholder(title, message)

    # -- industry mapping (in-GUI, no terminal) ------------------------

    def industry_prompt(self) -> dict:
        """The classification prompt for every still-unmapped company."""
        from cache import Cache
        from industry_worksheet import (build_prompt, unmapped,
                                        valid_industries)
        params = json.loads(config.PARAMS_PATH.read_text(encoding="utf-8"))
        params.setdefault("company_industry", {})
        valid = valid_industries(params)
        with Cache(self.db) as db:
            rows = unmapped(db, params, config.FISCAL_YEARS)
        return {"count": len(rows),
                "prompt": build_prompt(rows, valid) if rows else ""}

    def apply_industry_map(self, text: str) -> tuple[dict, int]:
        """Parse a pasted 'TICKER | Industry' reply into parameters.json."""
        import io
        from contextlib import redirect_stdout
        from cache import Cache
        from industry_worksheet import ingest, valid_industries

        if not (text or "").strip():
            return {"error": "Nothing pasted."}, 400
        if self.runner.busy:
            return {"error": "Something is running. Wait for it to finish, "
                             "then apply the mapping."}, 409
        params = json.loads(config.PARAMS_PATH.read_text(encoding="utf-8"))
        params.setdefault("company_industry", {})
        valid = valid_industries(params)
        if not valid:
            return {"error": "No industry list yet — refresh the Damodaran "
                             "industry data first."}, 400
        buf = io.StringIO()
        with Cache(self.db) as db, redirect_stdout(buf):
            summary = ingest(text, db, params, config.PARAMS_PATH, valid,
                             config.FISCAL_YEARS)
        summary["log"] = buf.getvalue()
        return summary, 200

    # -- credit spread ladder (fetch, show the diff, confirm) -----------

    def tables_check(self) -> dict:
        """
        Fetch Damodaran's two rating tables and diff them against
        refdata_tables.json. Nothing is written here -- the fetched
        result is only stashed on this App instance, so a later
        tables_apply() writes exactly what this call showed the user,
        never whatever a request happens to claim it found.
        """
        import urllib.error
        import fetch_ratings

        current = (json.loads(config.TABLES_PATH.read_text(encoding="utf-8"))
                  if config.TABLES_PATH.exists() else {})
        try:
            fetched = fetch_ratings.fetch()
        except (urllib.error.URLError, ValueError, TimeoutError) as e:
            return {"error": f"Could not fetch Damodaran's tables: {e}"}

        self._tables_fetch = fetched
        changes = fetch_ratings.diff(current, fetched)
        return {"changes": changes, "vintage_now": current.get("vintage")}

    def tables_apply(self) -> tuple[dict, int]:
        """Write the tables_check() result that is still stashed."""
        import fetch_ratings

        fetched = getattr(self, "_tables_fetch", None)
        if not fetched:
            return {"error": "Check for an update first."}, 400
        result = fetch_ratings.apply(config.TABLES_PATH, fetched)
        self._tables_fetch = None
        return {"vintage": result["vintage"]}, 200


class _LiveStderrHandler(logging.Handler):
    """
    A StreamHandler that looks up sys.stderr at emit time rather than
    binding it once, in __init__.

    Two things need that. First, tasks.py redirects sys.stderr per
    worker thread (see _Capture) so a running job's log lines land in
    the activity panel -- a handler bound at startup, before any job
    exists, would keep writing to the pre-job stream and never reach
    the GUI. Second, the packaged --noconsole .exe runs with sys.stderr
    set to None whenever no job is redirecting it; the stock
    logging.StreamHandler binds that None permanently at basicConfig()
    time, and every later log call then dies inside logging's own
    emit(), which is what produced a spurious "--- Logging error ---"
    dump ahead of the actual failure.
    """

    def emit(self, record: logging.LogRecord) -> None:
        stream = _sys.stderr
        if stream is None:
            return
        try:
            stream.write(self.format(record) + "\n")
        except Exception:
            pass


def free_port(preferred: int) -> int:
    """The preferred port, or any free one if it is taken."""
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port")


def main() -> int:
    p = argparse.ArgumentParser(description="Run the stock screen locally.")
    p.add_argument("--db", type=Path, default=config.DB_PATH)
    p.add_argument("--out-dir", type=Path, default=config.OUT_DIR)
    p.add_argument("--engine", choices=("auto", "excel", "libreoffice"),
                   default="auto")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--autostop-grace", type=float, default=10.0,
                   metavar="SECONDS",
                   help="Stop a running job this many seconds after the "
                        "dashboard window stops responding (close, crash, "
                        "sleep). 0 disables all window-watching. Default 10.")
    p.add_argument("--no-exit-on-close", action="store_true",
                   help="Keep the server running after the browser window "
                        "closes. By default it shuts down a short while "
                        "after, so closing the window frees the terminal.")
    p.add_argument("--no-daily-prices", action="store_true",
                   help="Do not auto-refresh held-portfolio prices once a "
                        "day while the app is running.")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        handlers=[_LiveStderrHandler()])

    # Resolve any paths the user gave relative to where they launched us,
    # then move into the project directory so every "./"-relative default
    # -- config's, and the ingestion scripts' own -- points at the real
    # data no matter where the app was started from. A wrong working
    # directory was the whole reason "parameters.json not found" and the
    # stages that depend on it were failing.
    args.db = args.db.resolve()
    args.out_dir = args.out_dir.resolve()
    os.chdir(config.ROOT)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    app = App(args.db, args.out_dir, args.engine,
              autostop_grace=max(0.0, args.autostop_grace),
              exit_on_close=not args.no_exit_on_close,
              daily_prices=not args.no_daily_prices)
    port = free_port(args.port)
    url = f"http://127.0.0.1:{port}/"

    server = ThreadingHTTPServer(("127.0.0.1", port),
                                 partial(Handler, app=app))
    server.daemon_threads = True
    # Called from the autostop thread when the window has been gone a while.
    app.on_close = server.shutdown

    # Dead-man switch: poll the window heartbeat, stop the job if it goes
    # quiet, then stop the server. Daemon thread, so it dies with the server.
    def _autostop_loop():
        while True:
            time.sleep(2.0)
            try:
                app.autostop_tick()
            except Exception:                        # noqa: BLE001
                log.exception("autostop check failed")

    if app.autostop_grace:
        threading.Thread(target=_autostop_loop, name="autostop",
                         daemon=True).start()

    # Held-portfolio prices refresh on open, then once a day while the app
    # keeps running. First check is a couple of seconds after startup --
    # off the main thread so it never delays the server coming up or the
    # browser opening -- then every 30 min; the per-day guard that keeps
    # this from re-fetching on every single launch lives in portfolio_meta,
    # so opening the app twice in one day is still just the one fetch.
    def _daily_prices_loop():
        time.sleep(2.0)
        while True:
            try:
                app.daily_price_tick()
            except Exception:                        # noqa: BLE001
                log.exception("daily price check failed")
            time.sleep(1800.0)

    if app.daily_prices:
        threading.Thread(target=_daily_prices_loop, name="daily-prices",
                         daemon=True).start()

    # Zero Terminal Policy: the packaged --noconsole build has no window
    # for these to appear in, and no one to press the Ctrl+C they mention.
    # They stay for `python app.py` from a real terminal.
    #
    # It is not just cosmetic: a frozen --noconsole build launches with
    # sys.stdout/sys.stderr set to None (there is no console to attach),
    # same as the logging crash above. A bare print() here would fail the
    # same way, except uncaught and on the main thread before
    # serve_forever() -- the whole app would never come up. FROZEN is
    # checked rather than "is sys.stdout None" so this can never depend on
    # exactly which stream a given PyInstaller build happens to leave usable.
    if not config.FROZEN:
        engine = recalc_engine()
        print(f"\n  Stock screen is running at  {url}")
        print(f"  Spreadsheet engine:         "
              f"{', '.join(engine['available']) or 'NONE — see Settings'}")
        print("  Press Ctrl+C to stop the server.")
        if app.exit_on_close:
            print(f"  Or just close the browser window — the server stops "
                  f"~{app.shutdown_grace:.0f}s later.")
        elif app.autostop_grace:
            print(f"  Closing the browser window stops a running job "
                  f"(after ~{app.autostop_grace:.0f}s).")
        if app.daily_prices:
            print("  Held-portfolio prices refresh on open, then once a day "
                  "while this keeps running.")
        print()

    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if not config.FROZEN:
            print("\n  Stopped.")
    else:
        if app._closing and not config.FROZEN:
            print("\n  Browser window closed — server stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
