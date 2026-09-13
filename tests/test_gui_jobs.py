"""
Dashboard job runner: the failure modes that made the app unusable.

1. A job script that calls sys.exit() must leave a terminal state, not
   wedge the runner on "running" forever (SystemExit is a BaseException).
2. A chatty script that never checks for cancellation itself must still be
   stoppable -- the output capture raises KeyboardInterrupt at its next
   print().
3. Closing the window (heartbeat goes silent) must stop a running job,
   after the grace period and not before, and never when disabled.
"""

import json
import time

import pytest

from tasks import TaskRunner
import app as appmod


def _wait_idle(runner, budget=6.0):
    end = time.time() + budget
    while runner.busy and time.time() < end:
        time.sleep(0.02)
    return runner.snapshot()


def test_sys_exit_does_not_wedge_the_runner():
    r = TaskRunner()

    def boom(_runner):
        raise SystemExit("no coverage CSV -- run esef_coverage first")

    assert r.start("extract", boom)
    s = _wait_idle(r)
    assert s["status"] == "failed"
    assert not r.busy
    # and the next job still starts
    assert r.start("next", lambda _r: {"ok": True})
    assert _wait_idle(r)["status"] == "done"


def test_clean_exit_is_success():
    r = TaskRunner()
    assert r.start("x", lambda _r: (_ for _ in ()).throw(SystemExit(0)))
    assert _wait_idle(r)["status"] == "done"


def test_chatty_script_is_cancellable_via_output_capture():
    r = TaskRunner()

    def chatty(_runner):
        for i in range(100_000):
            print(f"line {i}")          # never checks _runner.cancelled
            time.sleep(0.01)
        return {"done": True}

    assert r.start("coverage", chatty)
    time.sleep(0.3)
    assert r.busy
    assert r.cancel("stopped - the window was closed") is True
    s = _wait_idle(r)
    assert s["status"] == "cancelled"
    assert not r.busy


def test_autostop_tick_stops_a_stale_window():
    a = appmod.App(db=None, out_dir=None, engine="auto", autostop_grace=5.0)

    def slow(_runner):
        for _ in range(400):
            if _runner.cancelled:
                return {"cancelled": True}
            time.sleep(0.02)
        return {}

    assert a.runner.start("prices", slow)
    try:
        a.touch()
        a.autostop_tick()                       # fresh heartbeat -> no stop
        assert a.runner.busy

        a.last_seen -= 10.0                      # 10s of silence, grace is 5s
        a.autostop_tick()
        s = _wait_idle(a.runner)
        assert s["status"] == "cancelled"
        assert "window" in (s["detail"] or "")
    finally:
        a.runner.cancel()
        _wait_idle(a.runner)


def test_autostop_disabled_never_stops():
    a = appmod.App(db=None, out_dir=None, engine="auto", autostop_grace=0.0)
    assert a.runner.start("prices", lambda _r: time.sleep(0.5) or {})
    a.last_seen -= 999
    a.autostop_tick()
    assert a.runner.busy                         # grace 0 == disabled
    _wait_idle(a.runner)


# --- daily held-portfolio price refresh --------------------------------

def _one_open_portfolio(tmp_path, monkeypatch):
    import config
    from portfolio import Portfolio
    path = tmp_path / "p.db"
    monkeypatch.setattr(config, "PORTFOLIO_PATH", path)
    with Portfolio(path) as pf:
        pf.enter(lei="L1", ticker="AAA", name="Alpha", exchange="XPAR",
                 when="2026-06-01", price=10.0, value_per_share=15.0,
                 upside=0.5)
    return path


def test_daily_price_tick_refreshes_once_a_day(tmp_path, monkeypatch):
    from datetime import date
    from portfolio import Portfolio
    pf_path = _one_open_portfolio(tmp_path, monkeypatch)

    a = appmod.App(db=tmp_path / "f.db", out_dir=tmp_path, engine="auto",
                   autostop_grace=0.0)
    calls = []
    monkeypatch.setattr(a, "_fetch_prices",
                        lambda leis, since=None: calls.append((list(leis), since)) or True)

    a.daily_price_tick()
    assert len(calls) == 1
    leis, since = calls[0]
    assert leis == ["L1"]                          # fetched the held LEI
    assert since == "2026-06-01"                   # backfill from its entry date
    with Portfolio(pf_path) as pf:
        assert pf.prices_refreshed == date.today().isoformat()

    a.daily_price_tick()                           # same day -> no second fetch
    assert len(calls) == 1


def test_daily_price_tick_skips_when_busy_or_disabled(tmp_path, monkeypatch):
    _one_open_portfolio(tmp_path, monkeypatch)
    a = appmod.App(db=tmp_path / "f.db", out_dir=tmp_path, engine="auto",
                   autostop_grace=0.0)
    hits = []
    monkeypatch.setattr(a, "_fetch_prices", lambda leis, since=None: hits.append(1) or True)

    a.daily_prices = False
    a.daily_price_tick()
    assert not hits

    a.daily_prices = True
    a.runner.start("x", lambda _r: time.sleep(0.4) or {})
    try:
        a.daily_price_tick()
        assert not hits                           # a job is running
    finally:
        _wait_idle(a.runner)


def test_daily_price_tick_no_stamp_on_fetch_failure(tmp_path, monkeypatch):
    from portfolio import Portfolio
    pf_path = _one_open_portfolio(tmp_path, monkeypatch)
    a = appmod.App(db=tmp_path / "f.db", out_dir=tmp_path, engine="auto",
                   autostop_grace=0.0)
    monkeypatch.setattr(a, "_fetch_prices", lambda leis, since=None: False)

    a.daily_price_tick()
    with Portfolio(pf_path) as pf:
        assert pf.prices_refreshed is None       # not stamped -> retried later


# --- job_all -- the Dashboard's "Run everything" button -----------------

def _stub_steps(monkeypatch, calls, *, fail_at=None, cancel_at=None):
    """Replace every sub-step job_all() calls with a fake that just
    records its own name, in order -- so the chain, not any one stage's
    real work, is what gets exercised."""
    def make(label):
        def fn(_runner, **kw):
            if label == cancel_at:
                # The real job_prices()/job_screen() only return this
                # after noticing the runner was asked to stop -- so
                # reproduce that, not just the return shape.
                _runner.cancel()
                return {"cancelled": True}
            calls.append(label)
            if label == fail_at:
                raise RuntimeError(f"{label} blew up")
            return {"ok": label}
        return fn

    monkeypatch.setattr(appmod, "job_script",
                        lambda r, *, job, db: make(job)(r))
    monkeypatch.setattr(appmod, "job_prices", lambda r, **kw: make("prices")(r))
    monkeypatch.setattr(appmod, "job_shares", lambda r, **kw: make("shares")(r))
    monkeypatch.setattr(appmod, "job_screen", lambda r, **kw: make("screen")(r))


def test_job_all_runs_every_automatable_stage_in_order(tmp_path, monkeypatch):
    calls = []
    _stub_steps(monkeypatch, calls)
    r = TaskRunner()
    assert r.start("all", lambda run: appmod.job_all(
        run, db=tmp_path / "f.db", out_dir=tmp_path, engine="auto"))
    s = _wait_idle(r)
    assert s["status"] == "done"
    assert calls == ["coverage", "extract", "identity", "prices",
                     "shares", "parameters", "screen"]
    assert "industries" not in calls              # needs a person, skipped


def test_job_all_stops_at_the_first_failure(tmp_path, monkeypatch):
    calls = []
    _stub_steps(monkeypatch, calls, fail_at="prices")
    r = TaskRunner()
    assert r.start("all", lambda run: appmod.job_all(
        run, db=tmp_path / "f.db", out_dir=tmp_path, engine="auto"))
    s = _wait_idle(r)
    assert s["status"] == "failed"
    # coverage/extract/identity/prices ran (prices is where it blew up);
    # shares/parameters/screen never started
    assert calls == ["coverage", "extract", "identity", "prices"]


def test_job_all_stops_when_a_stage_reports_cancelled(tmp_path, monkeypatch):
    # job_prices()/job_screen() swallow their own KeyboardInterrupt and
    # return {"cancelled": True} instead of raising -- job_all() has to
    # notice that itself, not just a stop between stages.
    calls = []
    _stub_steps(monkeypatch, calls, cancel_at="shares")
    r = TaskRunner()
    assert r.start("all", lambda run: appmod.job_all(
        run, db=tmp_path / "f.db", out_dir=tmp_path, engine="auto"))
    s = _wait_idle(r)
    assert s["status"] == "cancelled"
    assert calls == ["coverage", "extract", "identity", "prices"]


def test_start_job_all_refuses_up_front_without_an_engine(tmp_path, monkeypatch):
    a = appmod.App(db=tmp_path / "f.db", out_dir=tmp_path, engine="auto",
                   autostop_grace=0.0)
    monkeypatch.setattr(appmod, "recalc_engine",
                        lambda: {"ok": False, "message": "no engine"})
    body, status = a.start_job("all", {})
    assert status == 400 and "no engine" in body["error"]
    assert not a.runner.busy


# --- tables_check / tables_apply -- the credit ladder confirm panel -----

def test_tables_check_stashes_the_fetch_and_returns_the_diff(tmp_path, monkeypatch):
    import config
    import fetch_ratings
    tables_path = tmp_path / "refdata_tables.json"
    tables_path.write_text(json.dumps({"vintage": "2020-01", "tables": {}}),
                           encoding="utf-8")
    monkeypatch.setattr(config, "TABLES_PATH", tables_path)
    fetched = {"LARGE": {"rows": []}, "SMALL": {"rows": []}}
    change = {"table": "LARGE", "row": 0, "was": None, "now": {},
             "text": "a change"}
    monkeypatch.setattr(fetch_ratings, "fetch", lambda: fetched)
    monkeypatch.setattr(fetch_ratings, "diff", lambda cur, new: [change])

    a = appmod.App(db=tmp_path / "f.db", out_dir=tmp_path, engine="auto",
                   autostop_grace=0.0)
    result = a.tables_check()
    assert result == {"changes": [change], "vintage_now": "2020-01"}
    assert a._tables_fetch is fetched          # stashed, not written


def test_tables_check_reports_a_fetch_error_without_crashing(tmp_path, monkeypatch):
    import config
    import fetch_ratings
    monkeypatch.setattr(config, "TABLES_PATH", tmp_path / "refdata_tables.json")

    def boom():
        raise ValueError("expected 15 rows, parsed 3")
    monkeypatch.setattr(fetch_ratings, "fetch", boom)

    a = appmod.App(db=tmp_path / "f.db", out_dir=tmp_path, engine="auto",
                   autostop_grace=0.0)
    result = a.tables_check()
    assert "expected 15 rows" in result["error"]
    assert a._tables_fetch is None             # nothing stashed to apply


def test_tables_apply_refuses_without_a_prior_check(tmp_path):
    a = appmod.App(db=tmp_path / "f.db", out_dir=tmp_path, engine="auto",
                   autostop_grace=0.0)
    body, status = a.tables_apply()
    assert status == 400 and "Check for an update first" in body["error"]


def test_tables_apply_writes_only_what_check_already_showed(tmp_path, monkeypatch):
    # apply() must never trust a request body's own numbers -- only the
    # fetch this App instance already fetched and returned to the page.
    import config
    tables_path = tmp_path / "refdata_tables.json"
    tables_path.write_text(json.dumps({
        "vintage": "2020-01", "tables": {},
        "size_cutoff": {"eur_millions": 4300, "basis": "fixed"}}),
        encoding="utf-8")
    monkeypatch.setattr(config, "TABLES_PATH", tables_path)

    a = appmod.App(db=tmp_path / "f.db", out_dir=tmp_path, engine="auto",
                   autostop_grace=0.0)
    a._tables_fetch = {"LARGE": {"rows": [{"coverage_from": 1.0}]},
                       "SMALL": {"rows": []}}
    body, status = a.tables_apply()
    assert status == 200 and "vintage" in body
    assert a._tables_fetch is None             # consumed, can't be re-applied

    written = json.loads(tables_path.read_text(encoding="utf-8"))
    assert written["tables"]["LARGE"]["rows"] == [{"coverage_from": 1.0}]
    assert written["size_cutoff"] == {"eur_millions": 4300, "basis": "fixed"}
