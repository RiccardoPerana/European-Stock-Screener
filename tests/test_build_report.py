"""
build_report -- the performance chart and the ticker grids on the results
report.
"""

import build_report as br


# --- _perf_chart / performance_series --------------------------------

def test_perf_chart_empty_state_is_a_framed_svg():
    assert br._perf_chart([]) == ""
    one = br._perf_chart([{"date": "2026-06-01", "return_pct": 0.0}])
    assert "<h2>" not in one            # heading lives on the summary side
    assert "<svg" in one and 'class="empty"' in one
    assert "Not Enough History" in one
    assert 'text-anchor="middle"' in one          # centred in the chart
    assert "<polyline" not in one                 # no line, just the frame
    assert "<p" not in one                        # no placeholder prose


def test_perf_chart_draws_a_line_with_no_endpoint_label():
    s = [{"date": "2026-06-01", "return_pct": 0.0},
         {"date": "2026-07-01", "return_pct": 0.08},
         {"date": "2026-08-01", "return_pct": -0.02},
         {"date": "2026-09-01", "return_pct": 0.15}]
    out = br._perf_chart(s)
    assert '<polyline class="line"' in out and '<path class="area"' in out
    assert 'class="end up"' in out            # endpoint marker still there
    assert 'class="lbl' not in out            # but no number on the line
    assert 'data-pts=' in out                 # hover still carries the value
    assert "<details" not in out              # no raw data table


def test_performance_series_equal_weight_over_time():
    from portfolio import performance_series, Portfolio
    import tempfile
    import pathlib
    with tempfile.TemporaryDirectory() as d:
        with Portfolio(pathlib.Path(d) / "p.db") as pf:
            pf.enter(lei="L1", ticker="A", name="A", exchange="X",
                     when="2026-06-01", price=10.0, value_per_share=15.0,
                     upside=0.5)
            pf.enter(lei="L2", ticker="B", name="B", exchange="X",
                     when="2026-07-01", price=20.0, value_per_share=30.0,
                     upside=0.5)
            hist = {"L1": [("2026-06-01", 10.0), ("2026-07-01", 11.0),
                           ("2026-08-01", 12.5)],
                    "L2": [("2026-07-01", 20.0), ("2026-08-01", 21.0)]}
            s = performance_series(pf, hist)
    by_date = {p["date"]: p for p in s}
    # 2026-06-01: only L1, flat
    assert by_date["2026-06-01"]["return_pct"] == 0.0
    assert by_date["2026-06-01"]["n"] == 1
    # 2026-08-01: (12.5/10-1 + 21/20-1)/2 = (0.25 + 0.05)/2
    assert abs(by_date["2026-08-01"]["return_pct"] - 0.15) < 1e-9
    assert by_date["2026-08-01"]["n"] == 2


# --- _ticker_grid -- "Cheap, but held back" / "Could not be valued" -----

def test_ticker_grid_empty_has_no_grid():
    out = br._ticker_grid([], lambda c: "", "Nothing held back.")
    assert out == '<p class="note muted">Nothing held back.</p>'
    assert "tickers" not in out


def test_ticker_grid_alphabetical_with_reason_on_the_button():
    companies = [{"ticker": "ZZZ", "name": "Zeta AG"},
                 {"ticker": "AAA", "name": "Alpha & Co"}]
    out = br._ticker_grid(companies, lambda c: f"why {c['ticker']}", "")
    assert out.index(">AAA<") < out.index(">ZZZ<")     # alphabetical
    assert 'data-name="Alpha &amp; Co"' in out          # attribute-escaped
    assert 'data-reason="why AAA"' in out
    assert '<div class="tick-detail" hidden></div>' in out
    assert "<table" not in out and "<td" not in out     # no table lines


def test_ticker_grid_js_toggles_on_the_same_ticker_and_closes_elsewhere():
    assert "document.addEventListener" in br.TICKER_GRID_JS
    assert "insertAdjacentElement" in br.TICKER_GRID_JS   # moves the detail
    assert "closeOpen" in br.TICKER_GRID_JS
