"""
track.RunFromJson -- the tracker must record the screen's own verdict, not
recompute one from value_per_share/price with its own copy of the 25%
margin-of-safety threshold (see pipeline.UNDERVALUED_THRESHOLD).
"""

from track import RunFromJson


class _FakeCompanyYear:
    def __init__(self, name):
        self.name = name


class _FakeDB:
    """Just enough of Cache's interface for RunFromJson."""

    def __init__(self, lei, ticker, exchange="XY", name="Test Co"):
        self._lei, self._ticker, self._exchange, self._name = (
            lei, ticker, exchange, name)

    def complete_entities(self, years):
        return [self._lei]

    def primary_listing(self, lei):
        return {"ticker": self._ticker, "exchange": self._exchange}

    def get(self, lei, fy0):
        return _FakeCompanyYear(self._name)


def test_recorded_verdict_is_used_verbatim_even_when_upside_disagrees():
    # upside here is +100%, which _verdict()'s own fallback would call
    # "UNDERVALUED - investigate" -- the recorded verdict must win anyway.
    doc = {
        "research_queue": [],
        "triggers": {
            "TICK": {"value_per_share": 100.0, "price": 50.0,
                     "verdict": "FAIRLY VALUED - no action"},
        },
    }
    run = RunFromJson(doc, _FakeDB("LEI1", "TICK"), [2021, 2022, 2023, 2024, 2025])
    assert run.companies[0].verdict == "FAIRLY VALUED - no action"


def test_missing_verdict_falls_back_to_recomputing_one():
    """A run_DATE.json from before "verdict" was added to triggers."""
    doc = {
        "research_queue": [],
        "triggers": {
            "TICK": {"value_per_share": 100.0, "price": 50.0},
        },
    }
    run = RunFromJson(doc, _FakeDB("LEI1", "TICK"), [2021, 2022, 2023, 2024, 2025])
    assert run.companies[0].verdict == "UNDERVALUED - investigate"


def test_verdict_static_fallback_thresholds():
    assert RunFromJson._verdict(0.30) == "UNDERVALUED - investigate"
    assert RunFromJson._verdict(-0.30) == "OVERVALUED - screen out"
    assert RunFromJson._verdict(0.0) == "FAIRLY VALUED - no action"
    assert RunFromJson._verdict(None) == "VOID - failed validation"



def test_iter_triggers_reads_lei_keyed_and_ticker_keyed_run_files():
    from track import iter_triggers

    current = {"triggers": {"LEI9": {"lei": "LEI9", "ticker": "AB"}}}
    legacy = {"triggers": {"AB": {"price": 1.0}}}
    lookup = {"AB": "LEI_FROM_DB"}.get

    assert [(lei, t) for lei, t, _ in iter_triggers(current, lookup)] ==         [("LEI9", "AB")]
    assert [(lei, t) for lei, t, _ in iter_triggers(legacy, lookup)] ==         [("LEI_FROM_DB", "AB")]


def test_research_flag_is_read_per_company_when_recorded():
    doc = {
        "research_queue": ["TICK"],     # stale queue list, ignored here
        "triggers": {
            "LEI1": {"lei": "LEI1", "ticker": "TICK", "value_per_share": 100.0,
                     "price": 50.0, "verdict": "UNDERVALUED - investigate",
                     "research_flag": False},
        },
    }
    run = RunFromJson(doc, _FakeDB("LEI1", "TICK"), [2021, 2022, 2023, 2024, 2025])
    assert run.companies[0].lei == "LEI1"
    assert run.companies[0].research_flag is False
