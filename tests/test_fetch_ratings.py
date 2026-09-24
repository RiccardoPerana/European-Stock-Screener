"""
fetch_ratings -- parsing Damodaran's two credit-spread pages, the diff
against refdata_tables.json, and apply(). fetch() itself (the network
calls) is exercised only through monkeypatched _fetch, never a live
request -- these run in CI with no network.
"""

import json

import pytest

import fetch_ratings as fr


def _row_tr(frm, to, rating_full, spread_pct):
    return (f"<tr><td>{frm}</td><td>{to}</td><td>{rating_full}</td>"
            f"<td>{spread_pct}%</td></tr>")


def _page(rows, *, second_table_rows=None):
    """A minimal stand-in for Damodaran's Excel-exported HTML: a header
    row (no '/' in it, so _ladder skips it) plus data rows, optionally
    with a second, unrelated <table> after it -- the large-cap page's
    shape, with the financial-service ladder in extra columns instead of
    a second table, is exercised separately in test_ladder_ignores_the_
    financial_service_columns."""
    header = "<tr><td>If interest coverage ratio is</td><td>&le; to</td>" \
             "<td>Rating is</td><td>Spread is</td></tr>"
    body = "".join(_row_tr(*r) for r in rows)
    out = f"<table>{header}{body}</table>"
    if second_table_rows:
        out += f"<table>{''.join(_row_tr(*r) for r in second_table_rows)}</table>"
    return f"<html><body>{out}</body></html>"


_FIFTEEN = [
    (-100000, 0.199999, "D2/D", "19.00"),
    (0.2, 0.649999, "C2/C", "16.00"),
    (0.65, 0.799999, "Ca2/CC", "12.61"),
    (0.8, 1.249999, "Caa/CCC", "8.85"),
    (1.25, 1.499999, "B3/B-", "5.09"),
    (1.5, 1.749999, "B2/B", "3.21"),
    (1.75, 1.999999, "B1/B+", "2.75"),
    (2.0, 2.249999, "Ba2/BB", "1.84"),
    (2.25, 2.499999, "Ba1/BB+", "1.38"),
    (2.5, 2.999999, "Baa2/BBB", "1.11"),
    (3.0, 4.249999, "A3/A-", "0.89"),
    (4.25, 5.499999, "A2/A", "0.78"),
    (5.5, 6.499999, "A1/A+", "0.70"),
    (6.5, 8.499999, "Aa2/AA", "0.55"),
    (8.5, 100000, "Aaa/AAA", "0.40"),
]


def test_ladder_skips_header_and_parses_every_data_row():
    rows = fr._first_table(_page(_FIFTEEN))
    ladder = fr._ladder(rows, from_col=0, rating_col=2, spread_col=3)
    assert len(ladder) == 15
    assert ladder[0] == {"coverage_from": -100000.0, "rating": "D",
                         "rating_full": "D2/D", "spread": 0.19}
    assert ladder[-1] == {"coverage_from": 8.5, "rating": "AAA",
                          "rating_full": "Aaa/AAA", "spread": 0.004}


def test_ladder_sorts_by_coverage_regardless_of_source_order():
    rows = fr._first_table(_page(list(reversed(_FIFTEEN))))
    ladder = fr._ladder(rows, from_col=0, rating_col=2, spread_col=3)
    assert [r["coverage_from"] for r in ladder] == \
        sorted(r["coverage_from"] for r in ladder)


def test_ladder_ignores_the_financial_service_columns():
    # The large-cap page's real shape: one <table>, financial-service
    # ladder sitting in columns 4-7 of the SAME rows, not a second
    # <table> -- reading only columns 0-3 must never pick those up.
    header = ("<tr><td>If interest coverage ratio is</td><td>&le; to</td>"
              "<td>Rating is</td><td>Spread is</td>"
              "<td>If long term interest coverage ratio is</td>"
              "<td>&le; to</td><td>Rating is</td><td>Spread is</td></tr>")
    row = ("<tr><td>-100000</td><td>0.199999</td><td>D2/D</td><td>19.00%</td>"
          "<td>-100000</td><td>0.049999</td><td>D2/D</td><td>99.99%</td></tr>")
    page = f"<html><body><table>{header}{row}</table></body></html>"
    rows = fr._first_table(page)
    ladder = fr._ladder(rows, from_col=0, rating_col=2, spread_col=3)
    assert ladder[0]["spread"] == 0.19          # not the 99.99% decoy


def test_first_table_raises_when_there_is_none():
    with pytest.raises(ValueError, match="no <table>"):
        fr._first_table("<html><body>nothing here</body></html>")


def test_fetch_raises_when_a_page_does_not_yield_fifteen_rows(monkeypatch):
    short = _FIFTEEN[:10]
    monkeypatch.setattr(fr, "_fetch",
                        lambda url: _page(short if url == fr.LARGE_URL
                                          else _FIFTEEN))
    with pytest.raises(ValueError, match="expected 15"):
        fr.fetch()


def test_fetch_returns_both_ladders_shaped_like_refdata_tables(monkeypatch):
    monkeypatch.setattr(fr, "_fetch", lambda url: _page(_FIFTEEN))
    out = fr.fetch()
    assert set(out) == {"LARGE", "SMALL"}
    assert len(out["LARGE"]["rows"]) == len(out["SMALL"]["rows"]) == 15
    assert "description" in out["LARGE"]


# --- diff() ---------------------------------------------------------------

_CURRENT = {"tables": {"LARGE": {"rows": [
    {"coverage_from": -100000, "rating": "D", "rating_full": "D2/D",
     "spread": 0.19},
    {"coverage_from": 0.2, "rating": "C", "rating_full": "C2/C",
     "spread": 0.16},
]}, "SMALL": {"rows": []}}}


def test_diff_is_empty_when_nothing_changed():
    fetched = {"LARGE": {"rows": _CURRENT["tables"]["LARGE"]["rows"]},
              "SMALL": {"rows": []}}
    assert fr.diff(_CURRENT, fetched) == []


def test_diff_reports_a_changed_spread():
    changed = [dict(_CURRENT["tables"]["LARGE"]["rows"][0]),
              dict(_CURRENT["tables"]["LARGE"]["rows"][1])]
    changed[1]["spread"] = 0.20                 # was 0.16
    fetched = {"LARGE": {"rows": changed}, "SMALL": {"rows": []}}
    changes = fr.diff(_CURRENT, fetched)
    assert len(changes) == 1
    assert changes[0]["table"] == "LARGE" and changes[0]["row"] == 1
    assert "16.00% → 20.00%" in changes[0]["text"]


def test_diff_reports_a_brand_new_row_with_no_was():
    fetched = {"LARGE": {"rows": _CURRENT["tables"]["LARGE"]["rows"]},
              "SMALL": {"rows": [
                  {"coverage_from": -100000, "rating": "D",
                   "rating_full": "D2/D", "spread": 0.14}]}}
    changes = fr.diff(_CURRENT, fetched)
    assert len(changes) == 1
    assert changes[0]["was"] is None and "new row" in changes[0]["text"]


# --- apply() ----------------------------------------------------------

def test_apply_writes_tables_and_stamps_vintage_without_touching_cutoff(tmp_path):
    import datetime
    path = tmp_path / "refdata_tables.json"
    path.write_text(json.dumps({
        "vintage": "2020-01", "next_expected": "2021-01",
        "size_cutoff": {"eur_millions": 4300, "basis": "fixed, do not float"},
        "tables": {"LARGE": {"rows": []}, "SMALL": {"rows": []}},
    }), encoding="utf-8")
    fetched = {"LARGE": {"rows": _FIFTEEN and
                         fr._ladder(fr._first_table(_page(_FIFTEEN)),
                                    from_col=0, rating_col=2, spread_col=3)},
              "SMALL": {"rows": []}}

    out = fr.apply(path, fetched, today=datetime.date(2027, 3, 15))

    assert out["vintage"] == "2027-03"
    assert out["next_expected"] == "2028-03"
    assert out["size_cutoff"] == {"eur_millions": 4300,
                                  "basis": "fixed, do not float"}
    assert len(out["tables"]["LARGE"]["rows"]) == 15
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == out
