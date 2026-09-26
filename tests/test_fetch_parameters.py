"""
fetch_parameters -- the Damodaran parsers and the guards around writing
parameters.json. Built from synthetic files; no network.
"""

import json

import openpyxl

import fetch_parameters as fp


def _industry_sheet(path, header, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Industry Averages"
    ws.append(["Date updated: January 2026"])     # preamble above the header
    ws.append([])
    ws.append(header)
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_unlevered_beta_skips_the_cash_corrected_column(tmp_path):
    # The cash-corrected column comes FIRST here, so a first-match parser
    # without the exclusion would pick it.
    path = tmp_path / "beta.xlsx"
    _industry_sheet(
        path,
        ["Industry Name", "Number of firms",
         "Unlevered beta corrected for cash", "Unlevered beta"],
        [["Machinery", 40, 0.95, 0.88],
         ["Total Market", 900, 0.80, 0.75]])
    out = fp.parse_industry_file(path, fp.INDUSTRY_SPECS["betaEurope.xls"])
    assert out == {"Machinery": {"unlevered_beta": 0.88}}


def _params():
    return {
        "market_wide": {"mature_market_erp": {"value": 0.05, "as_of": "x"}},
        "countries": {"_schema": {}, "IT": {"crp": 0.02, "tax_rate": 0.24}},
    }


def test_countries_in_percent_are_not_written(monkeypatch, tmp_path):
    monkeypatch.setattr(fp, "parse_ctryprem", lambda path: (
        {"IT": {"crp": 2.5, "tax_rate": 24.0, "total_erp": 7.0}}, 4.5,
        ["a country risk premium above 1.0 was read"]))
    params_path = tmp_path / "parameters.json"
    params = _params()

    assert fp.do_countries(params, tmp_path / "ctryprem.xlsx", params_path) == 1
    assert not params_path.exists()
    assert params["countries"]["IT"]["crp"] == 0.02


def test_a_column_not_found_keeps_the_stored_value(monkeypatch, tmp_path):
    # The CRP column was not matched this year; only the tax rate was read.
    monkeypatch.setattr(fp, "parse_ctryprem",
                        lambda path: ({"IT": {"tax_rate": 0.279}}, None, []))
    params_path = tmp_path / "parameters.json"

    assert fp.do_countries(_params(), tmp_path / "ctryprem.xlsx",
                           params_path) == 0
    written = json.loads(params_path.read_text(encoding="utf-8"))
    assert written["countries"]["IT"]["crp"] == 0.02
    assert written["countries"]["IT"]["tax_rate"] == 0.279
