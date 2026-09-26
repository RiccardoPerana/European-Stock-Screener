#!/usr/bin/env python3
"""
Background task runner
======================

Runs one long job at a time and reports its progress, so the interface
stays responsive while a screen is running.

WHY THIS IS SEPARATE FROM THE SERVER
------------------------------------
A screening run takes several minutes: 129 companies, each written to a
workbook, recalculated by LibreOffice or Excel, and read back. Doing that
inside a request handler would hold the connection open for the whole run,
the browser would time out, and the page could not show progress because
nothing would be sent until the work finished.

So the work happens on a worker thread and the page polls for state. The
server never blocks, and the dashboard can open instantly rather than
after the scan -- which is the behaviour that made a generated HTML file
awkward in the first place.

ONE JOB AT A TIME, ON PURPOSE
-----------------------------
Two screens running together would write to the same run directory and the
same database. The runner refuses to start a second job while one is
active, and says so, rather than allowing a race whose symptoms would be
half-written workbooks.
"""

from __future__ import annotations

import io
import logging
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class TaskState:
    """What the interface needs to draw a progress bar and a verdict."""

    name: str = ""
    status: str = "idle"          # idle | running | done | failed | cancelled
    done: int = 0
    total: int = 0
    detail: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    result: dict = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    @property
    def percent(self) -> int:
        return int(100 * self.done / self.total) if self.total else 0

    def to_dict(self) -> dict:
        return {
            "name": self.name, "status": self.status,
            "done": self.done, "total": self.total, "percent": self.percent,
            "detail": self.detail, "started_at": self.started_at,
            "finished_at": self.finished_at, "error": self.error,
            "result": self.result,
            # Only the tail: the full log of a 129-company run is thousands
            # of lines and the browser polls this several times a second.
            "log": self.log[-40:],
        }


class _Capture(io.TextIOBase):
    """
    Collects print() output from the ingestion scripts, which report their
    progress by printing, and shows it in the activity panel.

    ROUTED BY THREAD, NOT PROCESS-WIDE.
    sys.stdout is a single process-level object, so redirect_stdout()
    captures EVERY thread while the redirect is active -- including the
    HTTP server's own request logging, which would then appear in the
    activity panel as though the screen had printed it.

    So this proxy checks which thread is writing. The worker's output is
    captured; anything else passes through to the real stream untouched.

    IT IS ALSO THE CANCELLATION POINT.
    The ingestion scripts take no stop callback, but every one of them
    prints steadily -- per country, per company. So when a stop is
    requested, the next write() from the worker raises KeyboardInterrupt,
    which unwinds the script at its next print (none of them catch it) and
    lands in the runner's handler as "cancelled". The `_fired` latch makes
    it a one-shot, so the unwind itself is not interrupted again.
    """

    def __init__(self, sink: Callable[[str], None], owner: int,
                 passthrough, cancelled: Callable[[], bool] = lambda: False):
        self._sink = sink
        self._owner = owner
        self._passthrough = passthrough
        self._cancelled = cancelled
        self._fired = False
        self._buffer = ""

    def write(self, text: str) -> int:
        if threading.get_ident() != self._owner:
            return self._passthrough.write(text)
        if self._cancelled() and not self._fired:
            self._fired = True
            raise KeyboardInterrupt("stopped")
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._sink(line.rstrip())
        # A progress bar that only ever writes \r would otherwise never
        # flush; keep the tail bounded so it cannot grow without limit.
        if len(self._buffer) > 400:
            self._sink(self._buffer.strip())
            self._buffer = ""
        return len(text)

    def flush(self) -> None:
        try:
            self._passthrough.flush()
        except Exception:
            pass


class TaskRunner:
    """One job at a time, with progress the interface can poll."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.state = TaskState()
        # sys.stdout/sys.stderr are one process-global object each, so two
        # unrelated redirect_stdout() calls racing from different threads
        # can restore the wrong one on exit and leave a dead _Capture
        # permanently installed as the real stream. This is the only thing
        # allowed to swap them, whether the swap belongs to a tracked job
        # (below) or a piece of silent background housekeeping (app.py's
        # daily price refresh) that never touches `state` at all.
        self.stdio_lock = threading.Lock()

    # -- what the interface reads --------------------------------------

    @property
    def busy(self) -> bool:
        return self.state.status == "running"

    def snapshot(self) -> dict:
        with self._lock:
            return self.state.to_dict()

    # -- what a job calls to report itself ------------------------------

    def progress(self, done: int, total: int, detail: str = "") -> None:
        with self._lock:
            self.state.done, self.state.total = done, total
            if detail:
                self.state.detail = detail

    def say(self, line: str) -> None:
        with self._lock:
            self.state.log.append(line)
            # Bound the list itself, not just what is served: a long run
            # would otherwise hold every line it ever printed in memory.
            if len(self.state.log) > 400:
                del self.state.log[:200]

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- control --------------------------------------------------------

    def start(self, name: str, fn: Callable[["TaskRunner"], dict]) -> bool:
        """
        Begin a job. False if one is already running.

        `fn` receives this runner so it can call progress(), say() and
        check cancelled().
        """
        with self._lock:
            if self.state.status == "running":
                return False
            self._cancel.clear()
            self.state = TaskState(
                name=name, status="running",
                started_at=datetime.now(timezone.utc).isoformat(
                    timespec="seconds"))

        def wrapper():
            import sys
            capture = _Capture(self.say, threading.get_ident(),
                               sys.__stdout__, self._cancel.is_set)
            try:
                # Blocks only if the daily price tick's own brief redirect
                # (below) happens to be mid-flight; see stdio_lock above.
                with self.stdio_lock, \
                     redirect_stdout(capture), redirect_stderr(capture):
                    result = fn(self) or {}
                with self._lock:
                    self.state.status = ("cancelled" if self._cancel.is_set()
                                         else "done")
                    self.state.result = result
            except SystemExit as e:
                # The ingestion scripts still end with sys.exit() / argparse
                # p.error() on any bad input -- missing file, missing package,
                # nothing to do. SystemExit is a BaseException, not an
                # Exception, so without this branch it escapes the handler
                # below: the worker thread dies with status left on
                # "running", the job looks stuck forever, and `busy` stays
                # True so no other job can start. That is the hang.
                code = e.code
                quiet_exit = code is None or code == 0
                log.info("task %s exited (code %r)", name, code)
                with self._lock:
                    self.state.status = "done" if quiet_exit else "failed"
                    if not quiet_exit:
                        self.state.error = (
                            f"the script stopped: {code}"
                            if isinstance(code, str)
                            else f"the script exited with code {code}. The "
                                 f"activity log above says why.")
            except BaseException as e:                # noqa: BLE001
                # The task boundary must ALWAYS reach a terminal state --
                # anything left as "running" wedges the whole runner.
                if self._cancel.is_set():
                    # A stop we asked for: _Capture raised KeyboardInterrupt
                    # into the script, or the job noticed cancelled() itself.
                    with self._lock:
                        self.state.status = "cancelled"
                else:
                    log.exception("task %s failed", name)
                    with self._lock:
                        self.state.status = "failed"
                        self.state.error = f"{type(e).__name__}: {e}"
                        for line in traceback.format_exc().splitlines()[-12:]:
                            self.state.log.append(line)
            finally:
                with self._lock:
                    self.state.finished_at = datetime.now(
                        timezone.utc).isoformat(timespec="seconds")

        self._thread = threading.Thread(target=wrapper, name=name, daemon=True)
        self._thread.start()
        return True

    def cancel(self, reason: str = "") -> bool:
        """
        Ask the running job to stop.

        Cooperative: the job decides when to check. A screen checks between
        companies; the ingestion scripts stop at their next print(). Either
        way it stops within a second or two and leaves work already
        finished intact rather than half-written. `reason` is shown to the
        interface (e.g. when the window was closed).
        """
        if not self.busy:
            return False
        self._cancel.set()
        with self._lock:
            self.state.detail = reason or "stopping after the current item…"
        return True
