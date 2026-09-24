"""
Calendar-driven maintenance reminders (pipeline_status._calendar_reminders).

The brief asks for three notices the age-based staleness cannot express:
an annual Damodaran nudge in Jan/Feb, a quarterly "run the scan" nudge
when the calendar quarter turns, and the 180-day coverage alert (handled
by SCHEDULE, checked here too).
"""

from datetime import date

import pipeline_status as ps


def by_key(**over):
    base = {
        "tables": {"key": "tables", "health": "ok", "date": "2025-01"},
        "parameters": {"key": "parameters", "health": "ok",
                       "industries_date": "2025-06-15"},
        "screen": {"key": "screen", "health": "ok", "date": "2026-08-20"},
    }
    for k, v in over.items():
        base[k] = {**base[k], **v}
    return base


def keys(reminders):
    return {r["key"] for r in reminders}


def test_annual_damodaran_nudge_only_in_jan_feb_when_stale():
    # January, industry table + credit ladder both from last year -> nudge
    r = ps._calendar_reminders(by_key(), today=date(2026, 1, 15))
    assert "damodaran-annual" in keys(r)
    assert "Update required" in next(x["message"] for x in r
                                     if x["key"] == "damodaran-annual")

    # Same data, but it's September -> no annual nudge
    r = ps._calendar_reminders(by_key(), today=date(2026, 9, 15))
    assert "damodaran-annual" not in keys(r)


def test_annual_nudge_clears_once_refreshed_this_january():
    r = ps._calendar_reminders(
        by_key(parameters={"industries_date": "2026-01-08"},
               tables={"date": "2026-01"}),
        today=date(2026, 2, 1))
    assert "damodaran-annual" not in keys(r)


def test_annual_nudge_suppressed_when_ladder_already_blocking():
    r = ps._calendar_reminders(
        by_key(tables={"health": "blocking", "date": "2024-01"}),
        today=date(2026, 1, 20))
    assert "damodaran-annual" not in keys(r)


def test_quarterly_nudge_fires_on_a_new_calendar_quarter():
    # last scan in Q3, now Q4
    r = ps._calendar_reminders(by_key(screen={"date": "2026-08-01"}),
                               today=date(2026, 10, 2))
    assert "report-cycle" in keys(r)

    # last scan in Q3, still Q3 -> nothing
    r = ps._calendar_reminders(by_key(screen={"date": "2026-08-01"}),
                               today=date(2026, 9, 30))
    assert "report-cycle" not in keys(r)


def test_quarterly_nudge_not_piled_on_a_stale_screen():
    r = ps._calendar_reminders(
        by_key(screen={"date": "2026-01-01", "health": "stale"}),
        today=date(2026, 10, 2))
    assert "report-cycle" not in keys(r)


def test_coverage_stale_threshold_is_180_days():
    assert ps.SCHEDULE["coverage"] == 180
