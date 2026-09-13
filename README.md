# European equity valuation engine & portfolio tracker

An automated screen for European listed companies. It pulls financial
statements from the ESEF (XBRL) filing archive, prices from Yahoo Finance,
and risk parameters from Damodaran's datasets and the ECB, writes each
company into an Excel discounted-cash-flow model, reads the verdict back
out, and tracks the picks over time.

Everything runs on your own machine, via a minimalist local web dashboard.
Nothing is published anywhere.

Not investment advice. Returns are price only and exclude dividends.

---

## Requirements

- **Python 3.10+**
- A spreadsheet engine to recalculate the model — one of:
  - Microsoft Excel (Windows), via `pywin32`, or
  - LibreOffice, with `soffice` on `PATH` (any OS)
- `pip install -r requirements.txt`

## Quick start — the dashboard

```
python gui/app.py
```

Opens `http://127.0.0.1:8765/` in your browser. The dashboard shows every
pipeline stage, whether it is current, and a button to run it. Closing the
browser window stops the server; press Ctrl+C to stop it from the
terminal.

While the app is running it refreshes the prices of the companies
currently held in the portfolio once per calendar day (Yahoo, held
positions only — a handful, so it is quick). Disable with
`--no-daily-prices`.

Run it from the repository root. Paths (`financials.db`, `parameters.json`,
the template, `valuations/`) are resolved relative to the project, not the
working directory, but a few ingestion helpers still assume the root.

## The pipeline

Each stage feeds the next. A company can only be valued once it has five
complete fiscal years, a stock listing, a price, a share count and a
Damodaran industry.

| Stage | Script | What it does | Cadence |
|---|---|---|---|
| Coverage | `core/esef_coverage.py` | which EU companies filed five years of ESEF reports | annual |
| Extract | `core/esef_extract.py` | 20 financial line items × 5 years from each filing | annual |
| Identity | `core/resolve_identity.py` | LEI → ISIN → ticker; shares implied by EPS | annual |
| Prices | `core/fetch_prices.py` | one closing price per company (EUR only) | quarterly |
| Shares | `core/shares_worksheet.py` | diluted share count; hand-entered where derivation fails | annual |
| Parameters | `core/fetch_parameters.py` | risk-free rate (ECB), country risk and industry betas (Damodaran) | annual + quarterly rate |
| Industries | `core/industry_worksheet.py` | map each company to a Damodaran industry (LLM-assisted) | as needed |
| Screen | `core/run_screen.py` | write each company into the workbook, read the verdict | quarterly |
| Track | `core/track.py` | apply the run to the portfolio; mark to current prices | after each screen |

`core/screen.py` prints where you are and the next step to run.

## Valuation and signal logic

- **Fair value range:** intrinsic value ± 25%.
- **Undervalued (research flag):** market price < value × 0.75.
- **Exit target:** market price ≥ value (the midpoint).
- The portfolio opens a €100 position the first time a company is flagged
  undervalued and closes it when the verdict is no longer undervalued.

## Output

- `valuations/<date>/` — one workbook and one `*.provenance.json` per
  company, plus `run_<date>.json` and `report_<date>.html`.
- `output/` — relocated report exports (coverage scans, audits).

## Data sources and their terms

- **ESEF filings** — `filings.xbrl.org`, public.
- **Damodaran datasets** — NYU Stern, freely published for research.
- **ECB Data Portal** — public.
- **Yahoo Finance** — undocumented and **not licensed for programmatic
  access**. It is selected only via `--source yahoo`, never by default,
  and prices never leave this machine — nothing in this project publishes
  or redistributes them anywhere.

## Invariants

Do not change the DCF routines, the Excel cell formulas, or the five-year
fiscal window. The regression anchor is `DEMO.MI` `D83 = 11.4538271570406`.

```
python tests/golden_test.py --engine excel      # or --engine libreoffice
python -m pytest
```

`tests/golden_test.py` recalculates the shipped demo workbook and every
negative fixture. `pytest` covers the cache, the extractor, the CLI
entry points, the dashboard job runner, and the report sections.

## Layout

```
core/    pipeline: scrapers, ESEF parsing, the Excel engine, orchestration
gui/     the local dashboard (standard-library web app)
tests/   pytest suite + the golden gate + the synthetic-DB builder
tools/   R&D probes and one-off diagnostics
output/  generated report exports
```

## License

See [LICENSE](LICENSE).
