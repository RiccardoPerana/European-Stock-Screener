#!/usr/bin/env python3
"""
Build the HTML report
=====================

Turns a completed run into one self-contained page you can open by
double-clicking it.

    python build_report.py
    python build_report.py --run valuations/2026-09-07 --open

WHY IT IS A SINGLE FILE
-----------------------
The data is embedded in the page rather than fetched from a neighbouring
JSON file. Browsers block fetch() on file:// URLs, so a page that loads
its data at runtime works when served and fails silently when opened from
disk -- which is exactly how this will be opened. Embedding removes the
failure mode instead of documenting it.

Nothing is loaded from a CDN either: no webfonts, no chart library. The
page opens on a plane.

WHAT IT SHOWS
-------------
The queue first. On a terminal the research queue goes last, because four
minutes of output scrolls past and you want the answer where it lands. On
a page there is no scroll cost to reading the top, so the answer belongs
there, with the filtering underneath for anyone who wants to audit it.

Reads the provenance sidecars rather than results_DATE.xlsx: the sidecars
carry the check messages and the decision trail, which the spreadsheet
does not.
"""

from __future__ import annotations

import argparse
import html
import json
import webbrowser
from datetime import date
from pathlib import Path

import config

BAND = 0.25          # Valuation!D86 calls +/-25% fairly valued


def latest_run(out_dir: Path) -> Path | None:
    runs = sorted(d for d in out_dir.glob("*") if d.is_dir())
    return runs[-1] if runs else None


def load(run_dir: Path) -> dict:
    """Every sidecar in the run, plus the run summary."""
    companies = []
    for side in sorted(run_dir.glob("*.provenance.json")):
        try:
            doc = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        c, r, d = doc.get("company", {}), doc.get("results", {}), doc.get("decision", {})
        checks = doc.get("checks", [])
        companies.append({
            "ticker": c.get("ticker") or "?",
            "name": c.get("name") or "",
            "country": c.get("country") or "",
            "industry": c.get("industry") or "",
            "exchange": (doc.get("market_inputs", {}) or {}).get("price_mic") or "",
            "price": r.get("current_price"),
            "value": r.get("value_per_share"),
            "upside": r.get("upside"),
            "verdict": (r.get("verdict") or "").strip(),
            "wacc": r.get("wacc"),
            "rating": r.get("implied_rating"),
            "terminal": r.get("terminal_share_of_ev"),
            "margin_x": r.get("margin_vs_industry"),
            "roic_x": r.get("roic_vs_industry"),
            "flag": bool(d.get("research_flag")),
            "qualified": bool(d.get("qualifies_on_verdict")),
            "held_by": d.get("suppressor_messages") or [],
            "fails": [k["message"] for k in checks if k.get("severity") == "FAIL"],
            "warnings": [k["message"] for k in checks if k.get("severity") == "WARNING"],
        })

    def _read_run(json_path: Path) -> dict:
        try:
            return json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    summary = {}
    run_json = next(iter(run_dir.glob("run_*.json")), None)
    if run_json:
        summary = _read_run(run_json)

    return {"companies": companies, "summary": summary}


def exit_reason(verdict: str | None) -> str:
    """'OVERVALUED - screen out' -> 'Overvalued' -- the word before the
    dash, title-cased, for a short table/label context. The mirror of
    config.strip_severity(), which takes the other half of the same
    'VERDICT - reason' format."""
    return (verdict or "").split(" - ")[0].title()


def why(c: dict) -> str:
    bits = []
    if isinstance(c["wacc"], (int, float)):
        rating = f" ({c['rating']})" if c["rating"] else ""
        bits.append(f"discount rate {c['wacc'] * 100:.1f}%{rating}")
    if isinstance(c["terminal"], (int, float)):
        bits.append(f"{c['terminal'] * 100:.0f}% of the value sits beyond the forecast")
    if isinstance(c["margin_x"], (int, float)):
        bits.append(f"margins {c['margin_x']:.2f}× the industry")
    return ", ".join(bits)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

CSS = """
:root{
  /* Same tokens, same values as gui/ui.py: Paper #FBFBF9,
     Charcoal #222222, Muted Slate #4A5568 for the one accent (buttons). */
  --paper:#FBFBF9; --ink:#222222; --ink-soft:#5F5F5F;
  --rule:#DAD9D4; --rule-soft:#EDECE8;
  --go:#4A5568; --go-hi:#3B4353;
  /* Verdict colours: a semantic layer the base palette does not
     specify (see gui/ui.py's COLOUR note), so they keep their own hues. */
  --under:#2F6B4F; --over:#9B4A3C; --void:#8A8F8A; --band:#C9D6CE;
  --measure:66ch;
}
/* Follows the OS setting when opened from disk; the app's theme toggle
   sets data-theme to override it. Same palette as the rest of the app. */
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --paper:#1E1E1E; --ink:#E2E8F0; --ink-soft:#9BA3AE;
    --rule:#3A3A3A; --rule-soft:#2C2C2C;
    --go:#64748B; --go-hi:#7C8AA0;
    --under:#4E9B76; --over:#D08878; --void:#8A93A0; --band:#34473D;
  }
}
:root[data-theme="dark"]{
  --paper:#1E1E1E; --ink:#E2E8F0; --ink-soft:#9BA3AE;
  --rule:#3A3A3A; --rule-soft:#2C2C2C;
  --go:#64748B; --go-hi:#7C8AA0;
  --under:#4E9B76; --over:#D08878; --void:#7C837C; --band:#243A31;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-size:16px; line-height:1.55; font-variant-numeric:tabular-nums;
}
.wrap{max-width:960px; margin:0 auto; padding:2.5rem 1.5rem 6rem}
/* Identical to the app's other pages (gui/ui.py) so the header does not
   shift between Dashboard, Results and Portfolio. */
.masthead{display:flex; justify-content:space-between; align-items:baseline;
  gap:1rem; padding-bottom:.7rem; border-bottom:1px solid var(--ink);
  flex-wrap:wrap}
.masthead h1{font-size:1rem; font-weight:600; margin:0}
nav{display:flex; gap:1.25rem; font-size:.9375rem; align-items:baseline}
nav a{color:var(--ink-soft); text-decoration:none; padding-bottom:.15rem;
  border-bottom:1px solid transparent}
nav a:hover,nav a:focus-visible,nav a[aria-current]{
  color:var(--ink); border-bottom-color:var(--ink)}
#theme{background:transparent; color:var(--ink-soft); border:0;
  padding:.2rem .4rem; cursor:pointer; font-size:.9375rem; line-height:1}
#theme:hover{color:var(--ink)}
.runmeta{color:var(--ink-soft); font-size:.875rem; margin:.9rem 0 0}
.lede{margin:3rem 0 .5rem; font-size:clamp(1.9rem,4.4vw,2.9rem);
  line-height:1.12; font-weight:600; max-width:20ch; letter-spacing:-.015em}
.lede-sub{color:var(--ink-soft); margin:0 0 3rem; max-width:var(--measure)}
h2{font-size:1.0625rem; font-weight:600; margin:4rem 0 .25rem}
.note{color:var(--ink-soft); font-size:.9375rem; max-width:var(--measure)}
h2 + .note{margin:0 0 1.25rem}
/* Same solid button as the dashboard (gui/ui.py) -- an action button
   should not read as a different kind of control just because it lives
   on this page. */
button{font:inherit; color:var(--paper); background:var(--go); border:0;
  padding:.45rem 1rem; cursor:pointer; border-radius:2px}
button:hover:not(:disabled){background:var(--go-hi)}
button:disabled{background:var(--void); cursor:not-allowed}
.pick{padding:1.75rem 0; border-top:1px solid var(--rule)}
.pick:first-of-type{border-top:1px solid var(--ink)}
.pick-head{display:flex; justify-content:space-between; align-items:baseline;
  gap:1rem; flex-wrap:wrap}
.pick-name{font-size:1.25rem; font-weight:600; margin:0}
.pick-name span{color:var(--ink-soft); font-weight:400; font-size:.9375rem;
  margin-left:.5rem}
.pick-upside{font-size:1.25rem; font-weight:600; color:var(--under);
  white-space:nowrap}
.figures{display:flex; gap:2.5rem; margin:.75rem 0 1rem; flex-wrap:wrap}
.fig .k{display:block; font-size:.8125rem; color:var(--ink-soft)}
.fig .v{font-size:1.0625rem}
/* Top of the portfolio page: the summary figure on the left, the
   performance chart on the right; they stack on a narrow screen. The
   "Performance" title sits above the lede number on the left; the chart
   column carries a hidden twin of the same heading (same class, so an
   identical box) purely to hold that same vertical space, so the chart's
   top lands level with the lede number rather than with the title above
   it. align-items:stretch then makes both columns the height of the
   taller one, and the chart itself is pushed to the bottom of its
   column via flex-end, so its lower edge lands level with the figures
   row on the left rather than floating above it. */
.summary-row{display:flex; gap:2.5rem 3rem; align-items:stretch;
  flex-wrap:wrap; margin-top:.5rem}
.summary-row > .summary{flex:1 1 20rem; min-width:min(100%, 20rem)}
.summary-row > .perf-col{flex:1 1 24rem; min-width:min(100%, 22rem);
  display:flex; flex-direction:column}
.summary-row .perf-head{margin:0 0 .25rem}
.summary-row .perf-col .perf-head{visibility:hidden}
.summary-row .lede{margin-top:0}
.summary-row .lede-sub{max-width:100%; margin-bottom:1.5rem}
.summary-row .figures{margin:0}
.summary-row .perf-col .perf{margin-top:.5rem; flex:1; min-height:0;
  display:flex; flex-direction:column; justify-content:flex-end}
.perf{margin:1.5rem 0 .25rem}
/* pan-y, not none: a vertical swipe that starts on the chart still
   scrolls the page on a phone; a sideways drag scrubs the tooltip. */
.perf svg{width:100%; height:auto; display:block; touch-action:pan-y}
/* --k is set by PERF_FIT_JS on a narrow screen, where the 640-unit
   viewBox is drawn at about half size and would shrink its text with
   it; it scales the text back up to the size written here. */
.perf .empty{fill:var(--ink-soft); font-size:calc(14px * var(--k, 1)); font-weight:600}
.perf .base{stroke:var(--rule); stroke-width:1; stroke-dasharray:2 3;
  vector-effect:non-scaling-stroke}
.perf .area{fill:var(--rule-soft); opacity:.6}
.perf .area.neg{fill:var(--over)}
.perf .line{fill:none; stroke:var(--ink); stroke-width:2;
  stroke-linejoin:round; stroke-linecap:round; vector-effect:non-scaling-stroke}
.perf .end.up{fill:var(--under)} .perf .end.down{fill:var(--over)}
/* A return within rounding distance of 0.0% isn't a real gain or loss --
   overrides both the up/down area fill and the end marker to a neutral
   grey instead of red or green. */
.perf-flat .area.neg{fill:var(--rule-soft)}
.perf-flat .end.up,.perf-flat .end.down{fill:var(--ink-soft)}
.perf .axis{fill:var(--ink-soft); font-size:calc(11px * var(--k, 1))}
.perf .cursor{fill:var(--ink)}
.perf .tipbox{fill:var(--paper); stroke:var(--rule)}
.perf .tiptext{fill:var(--ink); font-size:calc(11px * var(--k, 1));
  font-variant-numeric:tabular-nums}
.pick-why{color:var(--ink-soft); font-size:.9375rem; margin:.25rem 0 0;
  max-width:var(--measure)}
.band{display:block; width:100%; height:52px; margin:.5rem 0 .25rem}
.band text{font-size:11px; fill:var(--ink-soft);
  font-family:inherit; font-variant-numeric:tabular-nums}
/* Scrolls sideways inside the page column if a table cannot fit a
   narrow screen, rather than widening the whole page (see also the
   scroll shadows in the max-width:620px block). */
.table-scroll{overflow-x:auto; -webkit-overflow-scrolling:touch; margin-top:.5rem}
/* The table's own top margin moves to the wrapper, where it still
   collapses with the heading or note above as it did unwrapped. */
.table-scroll > table{margin-top:0}
table{width:100%; border-collapse:collapse; margin-top:.5rem; font-size:.9375rem}
th{text-align:left; font-weight:600; font-size:.8125rem; color:var(--ink-soft);
  padding:.5rem .75rem .5rem 0; border-bottom:1px solid var(--ink)}
th.num,td.num{text-align:right}
td{padding:.7rem .75rem .7rem 0; border-bottom:1px solid var(--rule-soft);
  vertical-align:top}
td.reason{color:var(--ink-soft); width:46%}
/* A date never breaks at its hyphens ("2026-09-" / "24"). */
td.date{white-space:nowrap}
tr:last-child td{border-bottom:1px solid var(--rule)}
.t-name{font-weight:500}
.t-name small{display:block; color:var(--ink-soft); font-weight:400}
.up{color:var(--under)} .down{color:var(--over)} .muted{color:var(--void)}
/* "Cheap, but held back" / "Could not be valued": a wrapping row of
   ticker chips, no table lines. Clicking one opens a detail row -- a
   single element TICKER_GRID_JS moves to right after the LAST chip on
   the clicked one's own visual line (found by comparing offsetTop, since
   wrapping is responsive and not known ahead of time), and flex-basis:
   100% then forces it onto its own line below that. Inserting after the
   row rather than right after the clicked chip matters: it's what keeps
   every other chip still on that same row exactly where it was, instead
   of splitting the row at the click point and wrapping its back half
   down too. This is flex, not grid: CSS Grid's auto-placement algorithm
   runs a *separate, earlier* pass for any item with an explicit
   grid-column, so a grid-column:1/-1 detail row always claims the very
   first row regardless of where in the DOM it sits, shoving the whole
   grid down a line on every click -- flexbox has no such pass; items
   wrap in strict DOM order. `gap` keeps the spacing uniform on both
   axes regardless of how the tickers wrap. */
.tickers{--cols:10; display:flex; flex-wrap:wrap; align-items:flex-start;
  gap:.85rem; margin:.75rem 0 1rem}
/* Ten even columns, not content-sized chips: flex-basis is the row
   width minus nine gaps, divided by ten, so exactly ten land on a row
   regardless of how short or long any one ticker is -- min-width:0
   overrides flexbox's default of never shrinking below a chip's own
   content, which would otherwise let one long ticker bump the count on
   its row. font-size is set to fit that column comfortably. On narrower
   screens --cols drops (see the media queries below) so each column stays
   wide enough for its ticker; at ten columns on a phone they overlapped. */
.tick{font:inherit; font-size:.875rem; font-weight:600; padding:0;
  flex:0 0 calc((100% - (var(--cols) - 1) * .85rem) / var(--cols));
  min-width:0; border:0;
  background:none; color:var(--ink); cursor:pointer; text-align:center;
  white-space:nowrap}
/* Both need an explicit background, and .tickers .tick:hover rather
   than plain .tick:hover: the shared button{} rule above fills a solid
   --go-hi on hover (button:hover:not(:disabled)), and its selector --
   one element plus two pseudo-classes -- narrowly out-specifies a bare
   class plus :hover, so it was winning and painting each chip solid
   white-on-black or black-on-white on hover instead of just dimming
   the ticker text as intended. The extra .tickers ancestor here raises
   the specificity enough to override it. */
.tickers .tick:hover{color:var(--ink-soft); background:none}
.tickers .tick.open{color:var(--ink-soft); background:none}
.tick-detail{flex-basis:100%; padding:.65rem .8rem; margin:.1rem 0 .3rem;
  background:var(--rule-soft); border-radius:2px; font-size:.9375rem;
  color:var(--ink-soft)}
.tick-detail[hidden]{display:none}
.tick-detail strong{color:var(--ink); font-weight:600}
.funnel{margin:1.5rem 0 0; border-top:1px solid var(--ink)}
.funnel div{display:flex; justify-content:space-between; align-items:baseline;
  gap:1rem; padding:.85rem 0; border-bottom:1px solid var(--rule-soft)}
.funnel .n{font-size:1.375rem; font-weight:600; min-width:3.5ch;
  text-align:right}
.funnel .lbl{flex:1}
.funnel small{display:block; color:var(--ink-soft); font-size:.875rem}
.changed{margin:.75rem 0 0; border-top:1px solid var(--ink)}
.changed > div{display:flex; gap:1rem; align-items:baseline; padding:.8rem 0;
  border-bottom:1px solid var(--rule-soft)}
.changed .lbl{min-width:5.5rem; color:var(--ink-soft)}
.changed .val{flex:1}
.changed small{color:var(--ink-soft)}
footer{margin-top:5rem; padding-top:1.25rem; border-top:1px solid var(--rule);
  color:var(--ink-soft); font-size:.875rem}
footer p{margin:.4rem 0; max-width:var(--measure)}
/* The closing paragraph runs the full width of the page instead of the
   narrower reading column the rest of the footer keeps -- an explicit
   class on that one paragraph, not a :last-child guess. */
footer p.full{max-width:none}
a{color:inherit}
:focus-visible{outline:2px solid var(--ink); outline-offset:3px}
/* Below 800px the two .summary-row columns no longer fit side by side
   (their flex bases, 20rem + 24rem + the 3rem gap = 752px, plus the .wrap
   padding), so the chart stacks under the figures. The hidden twin
   heading then has no column to level with and would only leave a blank
   line above the chart. */
@media (max-width:799.98px){
  .summary-row .perf-col .perf-head{display:none}
  .summary-row .perf-col .perf{margin-top:0}
  .tickers{--cols:6}
  /* A ticker longer than its column wraps inside it rather than running
     over its neighbour. */
  .tick{white-space:normal; overflow-wrap:anywhere}
}
@media (max-width:620px){
  .wrap{padding:2rem 1.1rem 4rem}
  .figures{gap:1.5rem}
  td.reason{width:auto}
  nav{gap:1rem; flex-wrap:wrap}
  th,td{padding-right:.5rem}
  /* The usual scroll-shadow trick, for a table too wide for the screen: a
     soft shadow on whichever edge has more table beyond it; the paper-
     coloured covers scroll with the content and hide it once that edge is
     reached. Phones only -- no table overflows a wider page, and text over
     a gradient loses its subpixel smoothing. */
  .table-scroll{background:
    linear-gradient(to right, var(--paper) 60%, transparent) left/2rem 100% no-repeat local,
    linear-gradient(to left, var(--paper) 60%, transparent) right/2rem 100% no-repeat local,
    radial-gradient(farthest-side at 0 50%, rgba(0,0,0,.16), transparent) left/.8rem 100% no-repeat scroll,
    radial-gradient(farthest-side at 100% 50%, rgba(0,0,0,.16), transparent) right/.8rem 100% no-repeat scroll}
  /* iOS zooms the page in on any form control under 16px when it takes
     focus, and leaves it zoomed. */
  .sheet-picker select{font-size:16px}
}
@media (max-width:480px){
  .tickers{--cols:4}
}
@media print{body{background:#fff} .wrap{padding:0}}
/* Skins SheetJS's bare sheet_to_html() output for the methodology.html
   workbook preview. */
.sheet-picker{display:flex; align-items:baseline; gap:.75rem; margin:1rem 0}
.sheet-picker label{color:var(--ink-soft); font-size:.9375rem}
.sheet-picker select{font:inherit; font-size:.9375rem; color:var(--ink);
  background:var(--paper); border:1px solid var(--rule); border-radius:2px;
  padding:.3rem .5rem}
.sheet-wrap{overflow-x:auto; border:1px solid var(--rule); margin:.5rem 0 1.5rem}
.sheet-wrap table{border-collapse:collapse; font-size:.8125rem; white-space:nowrap}
.sheet-wrap td,.sheet-wrap th{border:1px solid var(--rule-soft);
  padding:.3rem .55rem; text-align:left; font-weight:400}
.sheet-wrap tr:first-child td,.sheet-wrap tr:first-child th{
  background:var(--rule-soft); font-weight:600}
"""

# The theme control. Kept identical to gui/ui.py (THEME_PREPAINT_JS /
# THEME_TOGGLE_JS) so light<->dark behaves the same here -- but inlined,
# because this file must also work opened straight from disk, with no
# server and nothing importable from gui/.
PREPAINT_JS = ("try{var t=localStorage.getItem('theme');"
               "if(t)document.documentElement.setAttribute("
               "'data-theme',t);}catch(e){}")

THEME_JS = """
(function(){
  var root = document.documentElement, btn = document.getElementById('theme');
  if (!btn) return;
  function shown(){
    var t = root.getAttribute('data-theme');
    if (t === 'dark' || t === 'light') return t;
    return (window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: dark)').matches)
      ? 'dark' : 'light';
  }
  function label(){
    var t = shown();
    btn.textContent = t === 'dark' ? '◑' : '◐';
    btn.title = (t === 'dark' ? 'Dark' : 'Light') + ' theme. Click to switch.';
  }
  btn.addEventListener('click', function(){
    var next = shown() === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try{ localStorage.setItem('theme', next); }catch(e){}
    label();
  });
  label();
})();"""


def band_svg(price, value, public: bool = False) -> str:
    """
    Where the price sits against fair value and the fairly-valued zone.

    The +/-25% band is the model's central device -- it is what turns a
    number into a verdict -- and it is invisible in every table. Drawing it
    shows at a glance how much room there is before the verdict changes,
    which a percentage alone does not.

    `public=True` drops the literal price/value figures from the
    aria-label; the picture itself is unchanged.
    """
    if not (isinstance(price, (int, float)) and isinstance(value, (int, float))
            and price > 0 and value > 0):
        return ""
    lo, hi = value / (1 + BAND), value / (1 - BAND)
    span_lo = min(price, lo) * 0.88
    span_hi = max(price, hi) * 1.06
    def x(v):
        return 2 + 96 * (v - span_lo) / (span_hi - span_lo)
    label = ("Price against fair value; the shaded zone is where the "
             "verdict would read fairly valued" if public else
             f"Price {price:,.2f} against fair value {value:,.2f}; "
             f"the shaded zone is where the verdict would read fairly valued")
    return f"""
<svg class="band" viewBox="0 0 100 22" preserveAspectRatio="none"
     role="img" aria-label="{esc(label)}">
  <line x1="2" y1="11" x2="98" y2="11" stroke="var(--rule)"
        stroke-width=".4" vector-effect="non-scaling-stroke"/>
  <rect x="{x(lo):.2f}" y="7" width="{max(x(hi) - x(lo), 0.4):.2f}" height="8"
        fill="var(--band)"/>
  <line x1="{x(value):.2f}" y1="4" x2="{x(value):.2f}" y2="18"
        stroke="var(--ink)" stroke-width="1"
        vector-effect="non-scaling-stroke"/>
  <circle cx="{x(price):.2f}" cy="11" r="1.1" fill="var(--under)"/>
</svg>"""


def band_labels(price, value, public: bool = False) -> str:
    """Not shown in public mode -- the whole line is a raw price figure."""
    if public:
        return ""
    if not (isinstance(price, (int, float)) and isinstance(value, (int, float))):
        return ""
    return (f'<p class="pick-why">Trading at {price:,.2f}. The model puts fair '
            f'value at {value:,.2f}, and would stop calling it undervalued '
            f'above {value / (1 + BAND):,.2f}.</p>')


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def pct(v, dp=0) -> str:
    return f"{v * 100:+.{dp}f}%" if isinstance(v, (int, float)) else "—"


def num(v, dp=2) -> str:
    return f"{v:,.{dp}f}" if isinstance(v, (int, float)) else "—"


def _ticker_grid(companies: list[dict], reason_of, empty_msg: str) -> str:
    """
    "Cheap, but held back" and "Could not be valued" -- a wrapping row of
    ticker chips, alphabetical, instead of a table. Clicking one opens a
    detail row underneath it with why it's listed; TICKER_GRID_JS moves a
    single hidden detail element to right after the last chip sharing the
    clicked one's own visual line and gives it `flex-basis:100%`, so it
    wraps onto its own line below that row without disturbing any chip
    still on it, and pushes only the rows that come after -- at any
    width, with no server-side layout math (see the .tickers CSS for why
    this is flex and not grid). `reason_of(c)` returns the plain-text
    explanation for one company.
    """
    if not companies:
        return f'<p class="note muted">{esc(empty_msg)}</p>'
    chips = "".join(
        f'<button type="button" class="tick" data-name="{esc(c["name"])}" '
        f'data-reason="{esc(reason_of(c))}">{esc(c["ticker"])}</button>'
        for c in sorted(companies, key=lambda x: x["ticker"]))
    return f"""
<div class="tickers">{chips}<div class="tick-detail" hidden></div></div>"""


TICKER_GRID_JS = """
(function(){
  var open = null;                    // the currently open .tick button
  function closeOpen(){
    if (!open) return;
    var detail = open.parentElement.querySelector('.tick-detail');
    if (detail){ detail.hidden = true; detail.textContent = ''; }
    open.classList.remove('open');
    open = null;
  }
  function rowEnd(btn){
    // The last chip that shares btn's visual line, so the detail panel
    // opens after the whole row rather than splitting it -- everything
    // still to the right of btn stays put; only the rows below move.
    var top = btn.offsetTop, last = btn, next = btn.nextElementSibling;
    while (next && next.classList.contains('tick') &&
           Math.abs(next.offsetTop - top) < 1){
      last = next;
      next = next.nextElementSibling;
    }
    return last;
  }
  function openTick(btn){
    var grid = btn.closest('.tickers');
    var detail = grid && grid.querySelector('.tick-detail');
    if (!detail) return;
    detail.textContent = '';
    var name = btn.getAttribute('data-name');
    if (name){
      var strong = document.createElement('strong');
      strong.textContent = name;
      detail.appendChild(strong);
      detail.appendChild(document.createTextNode(' \\u2014 '));
    }
    detail.appendChild(
      document.createTextNode(btn.getAttribute('data-reason') || ''));
    rowEnd(btn).insertAdjacentElement('afterend', detail);
    detail.hidden = false;
    btn.classList.add('open');
    open = btn;
  }
  document.addEventListener('click', function(e){
    var btn = e.target.closest && e.target.closest('.tick');
    if (!btn){ closeOpen(); return; }
    var same = btn === open;
    closeOpen();
    if (!same) openTick(btn);
  });
})();"""


def _nav_links(current: str) -> str:
    """Nav links between the published pages. Only used when nav_links=True."""
    def link(href: str, label: str, key: str) -> str:
        current_attr = ' aria-current="page"' if key == current else ''
        return f'<a href="{href}"{current_attr}>{label}</a>'
    return (link("index.html", "Results", "results") +
            link("portfolio.html", "Portfolio", "portfolio") +
            link("methodology.html", "Methodology", "methodology"))


def render(data: dict, public: bool = False, nav_links: bool = False) -> str:
    """
    `public=True` drops every EUR price/fair-value figure (see
    band_svg/band_labels); upside percentages, verdicts and the
    WACC/margin "why" text stay.
    """
    cs = data["companies"]
    summary = data["summary"]
    queue = [c for c in cs if c["flag"]]
    held = [c for c in cs if c["qualified"] and not c["flag"]]
    void = [c for c in cs if c["fails"]]
    under = [c for c in cs if c["qualified"]]
    when = summary.get("valuation_date") or date.today().isoformat()
    years = summary.get("fiscal_years") or config.FISCAL_YEARS

    picks = []
    for c in sorted(queue, key=lambda x: -(x["upside"] or 0)):
        where = " · ".join(p for p in (c["country"], c["industry"]) if p)
        picks.append(f"""
<article class="pick">
  <div class="pick-head">
    <h3 class="pick-name">{esc(c['name'] or c['ticker'])}
      <span>{esc(c['ticker'])}{' — ' + esc(where) if where else ''}</span></h3>
    <div class="pick-upside">{pct(c['upside'])}</div>
  </div>
  {band_svg(c['price'], c['value'], public)}
  {band_labels(c['price'], c['value'], public)}
  <p class="pick-why">{esc(why(c))}</p>
</article>""")

    def held_reason(c):
        why_bits = " · ".join(config.strip_severity(m)
                              for m in c["held_by"]) or "—"
        return f"{pct(c['upside'])} upside · {why_bits}"

    def void_reason(c):
        return " · ".join(config.strip_severity(m)
                          for m in c["fails"]) or "—"

    held_grid = _ticker_grid(held, held_reason, "Nothing held back.")
    void_grid = _ticker_grid(void, void_reason, "Everything valued cleanly.")

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Stock screen — {esc(when)}</title>
<script>{PREPAINT_JS}</script>
<style>{CSS}</style>
</head><body>
<div class="wrap">

<header class="masthead">
  <h1>European stock screen</h1>
  <nav>{_nav_links('results') if nav_links else ''}<button id="theme" type="button"
       title="Switch between light and dark"
       aria-label="Switch between light and dark">◐</button></nav>
</header>
<p class="runmeta">Screen run {esc(when)} · {len(cs)} companies valued ·
financial years {years[0]}–{years[-1]}</p>

<p class="lede">{"Nothing to research this quarter." if not queue
  else f"{len(queue)} worth researching." if len(queue) > 1
  else "One worth researching."}</p>
<p class="lede-sub">Out of {len(cs)} companies valued from their published
accounts. {"None" if not under else len(under)} looked cheap{"" if not under else
f"; {len(under) - len(queue)} of those carry a warning that has to be "
f"understood before the number means anything" if len(under) > len(queue)
else " and cleared every check"}.</p>

{"".join(picks) if picks else
 '<p class="lede-sub">Nothing cleared the filter this quarter. That is a '
 'result, not a failure — but if it repeats, check the upside distribution '
 'before trusting the thresholds.</p>'}

<h2>How {len(cs)} became {len(queue)}</h2>
<div class="funnel">
  <div><span class="lbl">Valued from their accounts</span>
       <span class="n">{len(cs)}</span></div>
  <div><span class="lbl">Passed every validity check
       <small>the rest are listed below</small></span>
       <span class="n">{len(cs) - len(void)}</span></div>
  <div><span class="lbl">Priced below fair value by more than 25%</span>
       <span class="n">{len(under)}</span></div>
  <div><span class="lbl">And carried no warning</span>
       <span class="n">{len(queue)}</span></div>
</div>

<h2>Cheap, but held back</h2>
<p class="note">These read as undervalued. Each also tripped a warning, so
the model will not stand behind the number without a human looking at the
reason. Click a ticker for why.</p>
{held_grid}

<h2>Could not be valued</h2>
<p class="note">A check failed, so the workbook disowns every number for
these — not even as a rough estimate. Usually the company lost money, or an
input was missing from the filing. Click a ticker for why.</p>
{void_grid}

<footer>
  <p class="full">Fair value is a discounted cash flow from five years of
  published accounts, margins normalised to the five-year median. The
  model cannot recognise a durable competitive advantage, so quality
  compounders read expensive. Not investment advice.</p>
  {'<p class="full">Prices and EUR fair-value figures are omitted here — '
   'Yahoo Finance, the source, is not licensed for redistribution. Upside '
   'percentages are the model'"'"'s own output, not republished price data.</p>'
   if public else ''}
</footer>

</div>
<script>{THEME_JS}
{TICKER_GRID_JS}</script>
</body></html>"""


def eur(v) -> str:
    return f"€{v:,.2f}" if isinstance(v, (int, float)) else "—"


PERF_JS = """
(function(){
  // On a narrow screen the 640-unit viewBox is drawn at about half size,
  // which shrinks the axis labels, tooltip and markers along with it. k is
  // viewBox units per CSS pixel there, so a size times k is drawn at that
  // size on screen; wider screens keep k = 1 and look as they always have.
  var narrow = window.matchMedia && window.matchMedia('(max-width:620px)');
  function scaleOf(svg){
    var w = svg.getBoundingClientRect().width;
    return narrow && narrow.matches && w ? svg.viewBox.baseVal.width / w : 1;
  }
  function fit(){
    var all = document.querySelectorAll('.perf svg');
    for (var i = 0; i < all.length; i++){
      var k = scaleOf(all[i]), end = all[i].querySelector('.end');
      all[i].style.setProperty('--k', k);          // text: see the .perf CSS
      if (end) end.setAttribute('r', 3.5 * k);
    }
  }
  fit();
  window.addEventListener('resize', fit);

  var box = document.querySelector('.perf[data-pts]');
  if (!box) return;
  var pts; try { pts = JSON.parse(box.getAttribute('data-pts')); }
  catch (e) { return; }
  if (!pts || pts.length < 2) return;
  var svg = box.querySelector('svg'), vb = svg.viewBox.baseVal;
  var cur = box.querySelector('.cursor'), tip = box.querySelector('.tip');
  var rect = box.querySelector('.tipbox'), txt = box.querySelector('.tiptext');
  function xIn(ev){
    var r = svg.getBoundingClientRect();
    var cx = ev.touches ? ev.touches[0].clientX : ev.clientX;
    return (cx - r.left) / r.width * vb.width;
  }
  function show(ev){
    var x = xIn(ev), best = pts[0], bd = 1e9, k = scaleOf(svg);
    for (var i = 0; i < pts.length; i++){
      var d = Math.abs(pts[i][0] - x);
      if (d < bd){ bd = d; best = pts[i]; }
    }
    cur.setAttribute('cx', best[0]); cur.setAttribute('cy', best[1]);
    cur.setAttribute('r', 3 * k);
    cur.style.display = ''; tip.style.display = '';
    txt.textContent = best[2] + '   ' + best[3];
    var w = txt.getComputedTextLength() + 12 * k, h = 18 * k;
    var tx = Math.min(Math.max(best[0] - w / 2, 2), vb.width - w - 2);
    var ty = Math.max(best[1] - 26 * k, 2);
    rect.setAttribute('x', tx); rect.setAttribute('y', ty);
    rect.setAttribute('width', w); rect.setAttribute('height', h);
    txt.setAttribute('x', tx + 6 * k); txt.setAttribute('y', ty + 13 * k);
  }
  function hide(){ cur.style.display = 'none'; tip.style.display = 'none'; }
  svg.addEventListener('mousemove', show);
  svg.addEventListener('mouseleave', hide);
  // Passive, and no preventDefault: the svg's touch-action:pan-y leaves
  // vertical swipes to scroll the page. A tap shows that day's value and
  // it stays up until the next touch somewhere else.
  svg.addEventListener('touchstart', show, {passive:true});
  svg.addEventListener('touchmove', show, {passive:true});
  document.addEventListener('touchstart', function(e){
    if (!svg.contains(e.target)) hide();
  }, {passive:true});
})();"""


def _perf_chart(series: list[dict]) -> str:
    """
    An inline-SVG line of portfolio return over time. No library: the page
    has to open straight from disk. `series` is performance_series() output.
    With fewer than two points the frame still renders, with "Not Enough
    History" centred in it. The "Performance" heading is not part of this
    markup -- it sits over the summary figure on the left of the row
    instead, so the chart is free to fill the full height beside it; the
    caller adds a matching hidden heading of its own so the two columns
    still start level (see render_portfolio()).
    """
    if not series:
        return ""
    # H was 200 -- flat and noticeably shorter than the summary column
    # beside it even after the two were top- and bottom-aligned (see the
    # CSS notes on .summary-row). Taller now so the chart actually fills
    # more of that column instead of just sitting flush with its edges.
    W, H = 640, 300
    # R only has to clear the end-of-line marker; the value label sits
    # above the point rather than out to the right of it (see end_lbl_y
    # below), so the plotted line can run almost to the right edge of the
    # chart instead of leaving a wide gap before it.
    # T is small: there is no "0%" axis label to leave headroom for any
    # more (the dashed baseline itself marks zero), so the plotted line
    # can run almost to the top of the chart.
    L, R, T, B = 10, 14, 4, 22

    if len(series) < 2:
        mid_y = T + (H - T - B) / 2
        return f"""
<div class="perf perf-empty">
<svg viewBox="0 0 {W} {H}" role="img"
     aria-label="Portfolio performance — not enough history yet">
  <line class="base" x1="{L}" y1="{mid_y:.0f}" x2="{W - R}" y2="{mid_y:.0f}"/>
  <text class="empty" x="{(L + W - R) / 2:.0f}" y="{H / 2:.0f}"
        text-anchor="middle" dominant-baseline="middle">Not Enough History</text>
</svg>
</div>"""
    xs = [date.fromisoformat(s["date"]).toordinal() for s in series]
    rs = [s["return_pct"] for s in series]
    x0, x1 = xs[0], xs[-1]
    span_x = (x1 - x0) or 1
    lo, hi = min(0.0, min(rs)), max(0.0, max(rs))
    if hi == lo:
        lo, hi = -0.01, 0.01
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad

    def X(o):
        return L + (o - x0) / span_x * (W - L - R)

    def Y(r):
        return T + (hi - r) / (hi - lo) * (H - T - B)

    px = [(X(xs[i]), Y(rs[i])) for i in range(len(series))]
    base_y = Y(0.0)
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in px)
    area = (f"M {px[0][0]:.1f},{base_y:.1f} L {line} "
            f"L {px[-1][0]:.1f},{base_y:.1f} Z")
    end_cls = "up" if rs[-1] >= 0 else "down"
    # A return this close to zero isn't really a gain or a loss -- rounds to
    # 0.0% at the one decimal place the aria-label and tooltips already use.
    # Colouring the whole chart red or green over-reads noise that small, so
    # the shading (both the fill under the line and the end marker) goes
    # neutral instead of up/down -- see the .perf-flat CSS.
    flat = abs(rs[-1]) < 0.0005

    def short(d):
        dt = date.fromisoformat(d)
        return dt.strftime("%b %d").replace(" 0", " ")

    pts_json = json.dumps([[round(x, 1), round(y, 1), s["date"],
                            f"{s['return_pct'] * 100:+.1f}%"]
                           for (x, y), s in zip(px, series)])

    return f"""
<div class="perf{' perf-flat' if flat else ''}" data-pts='{esc(pts_json)}'>
<svg viewBox="0 0 {W} {H}" role="img"
     aria-label="Portfolio return from {esc(series[0]['date'])} to
     {esc(series[-1]['date'])}, ending {rs[-1] * 100:+.1f} percent">
  <defs>
    <clipPath id="perf-pos"><rect x="0" y="0" width="{W}" height="{base_y:.1f}"/></clipPath>
    <clipPath id="perf-neg"><rect x="0" y="{base_y:.1f}" width="{W}"
                                  height="{H - base_y:.1f}"/></clipPath>
  </defs>
  <path class="area" d="{area}" clip-path="url(#perf-pos)"/>
  <path class="area neg" d="{area}" clip-path="url(#perf-neg)"/>
  <line class="base" x1="{L}" y1="{base_y:.1f}" x2="{W - R}" y2="{base_y:.1f}"/>
  <polyline class="line" points="{line}"/>
  <circle class="end {end_cls}" cx="{px[-1][0]:.1f}" cy="{px[-1][1]:.1f}" r="3.5"/>
  <text class="axis" x="{L}" y="{H - 6}">{esc(short(series[0]['date']))}</text>
  <text class="axis" x="{px[-1][0]:.0f}" y="{H - 6}"
        text-anchor="end">{esc(short(series[-1]['date']))}</text>
  <circle class="cursor" r="3" style="display:none"/>
  <g class="tip" style="display:none">
    <rect class="tipbox" rx="2" width="0" height="18"/>
    <text class="tiptext" x="0" y="0"></text>
  </g>
</svg>
</div>"""


def _changed_block(positions: list[dict], last_scan: str | None, *,
                   with_reason: bool) -> str:
    """
    "What changed" -- the companies added or dropped at the most recent
    scan. A position moved at that scan iff its entry (or exit) date is the
    scan's date, which is what portfolio.sync() stamps them with.
    """
    if not last_scan:
        return ""
    added = [p for p in positions
             if p["status"] == "open" and p.get("entry_date") == last_scan]
    gone = [p for p in positions
            if p["status"] == "closed" and p.get("exit_date") == last_scan]

    head = '<h2>What changed from the last scan</h2>'
    if not added and not gone:
        return head + ('\n<p class="note">No company entered or left the '
                       'book.</p>')

    def entry(p):
        return f"{esc(p['ticker'])} <small>{esc(p['name'])}</small>"

    def leave(p):
        if not with_reason:
            return entry(p)
        word = exit_reason(p.get("exit_verdict"))
        tail = f" — {esc(word)}" if word else ""
        return f"{esc(p['ticker'])} <small>{esc(p['name'])}{tail}</small>"

    return head + f"""
<div class="changed">
  <div><span class="lbl">Added</span>
       <span class="val">{", ".join(entry(p) for p in added) or "—"}</span></div>
  <div><span class="lbl">Removed</span>
       <span class="val">{", ".join(leave(p) for p in gone) or "—"}</span></div>
</div>"""


def render_portfolio(marked: dict, public: bool = False,
                     nav_links: bool = False) -> str:
    """
    The held book and how it has done, in the same style as the results
    report. `marked` is the dict from portfolio.mark(): 100 EUR is
    committed to each company the day it enters the research queue, and a
    position closes when the verdict is no longer undervalued.

    The header figure is the weighted-average price return of the companies
    held now. The stakes are equal, so that is simply the mean of each
    holding's return; closed positions are shown for the record but do not
    count toward it.

    `public=True` drops the "Held now" table's Entry/Now price columns.
    Everything else -- return %, Invested/Value-now, the performance
    chart, the Sold table -- stays; none of it is a quoted price.
    """
    from portfolio import STAKE

    rows = marked.get("positions") or []
    held = [p for p in rows if p["status"] == "open"]
    sold = [p for p in rows if p["status"] == "closed"]
    n = len(held)

    overall = sum(p["return_pct"] for p in held) / n if n else 0.0
    invested = STAKE * n
    value_now = sum(p["value"] for p in held)
    ov_cls = "up" if overall >= 0 else "down"

    when = marked.get("as_of") or date.today().isoformat()
    since = marked.get("inception_date")
    missing = marked.get("missing_price") or []
    voids = [p["ticker"] for p in held if p.get("unvaluable")]

    def ret_cell(p):
        cls = "up" if p["return_pct"] >= 0 else "down"
        return f'<td class="num {cls}">{pct(p["return_pct"], 1)}</td>'

    def held_row(p):
        price_cells = "" if public else (f'<td class="num">{num(p["entry_price"])}</td>'
                                          f'<td class="num">{num(p["price"])}</td>')
        return (f'<tr><td class="t-name">{esc(p["ticker"])}'
                f'<small>{esc(p["name"])}</small></td>'
                f'<td class="date">{esc(p["entry_date"])}</td>'
                f'{price_cells}{ret_cell(p)}</tr>')

    held_rows = "".join(held_row(p)
                        for p in sorted(held, key=lambda x: -x["return_pct"]))

    sold_rows = "".join(f"""
<tr><td class="t-name">{esc(p['ticker'])}<small>{esc(p['name'])}</small></td>
    <td class="date">{esc(p['entry_date'])}</td>
    <td class="date">{esc(p['exit_date'] or '')}</td>
    {ret_cell(p)}
    <td class="reason">{esc(exit_reason(p['exit_verdict']))}</td>
</tr>""" for p in sorted(sold, key=lambda x: (x['exit_date'] or '', x['ticker'])))

    lede = f"{overall * 100:+.1f}%" if n else "Nothing held yet."
    if n:
        sub = (f"Weighted-average price return of the {n} "
               f"{'company' if n == 1 else 'companies'} held now — €100 in "
               f"each since the day it entered the research queue, weighted "
               f"equally. Price only, before dividends.")
    else:
        sub = ("A company joins the day the screen first flags it as "
               "undervalued, with €100 committed at that day's price, and "
               "leaves when the verdict is no longer undervalued. Run a "
               "scan, then check back here.")

    figures = "" if not n else f"""
<div class="figures">
  <div class="fig"><span class="k">Companies held</span>
      <span class="v">{n}</span></div>
  <div class="fig"><span class="k">Invested</span>
      <span class="v">{eur(invested)}</span></div>
  <div class="fig"><span class="k">Value now</span>
      <span class="v">{eur(value_now)}</span></div>
</div>"""

    changed = _changed_block(rows, marked.get("last_scan_date"),
                             with_reason=True)
    perf = _perf_chart(marked.get("series") or [])
    # The heading sits over the lede number, not over the chart; the
    # chart column gets a hidden twin of it purely to hold the same
    # vertical space (see the .perf-head CSS).
    perf_head = '<h2 class="perf-head">Performance</h2>' if perf else ""
    perf_col = (f'<div class="perf-col">'
                f'<h2 class="perf-head" aria-hidden="true">Performance</h2>'
                f'{perf}</div>') if perf else ""

    missing_note = "" if not missing else (
        f'<p class="note">{len(missing)} '
        f'{"position is" if len(missing) == 1 else "positions are"} held at '
        f'cost — no current price ({esc(", ".join(missing[:10]))}). Marked '
        f'at entry rather than zero so a data gap does not read as a loss.'
        f'</p>')
    void_note = "" if not voids else (
        f'<p class="note">The model cannot currently value '
        f'{esc(", ".join(voids))} — held, not sold, because a void is a '
        f'failure to measure rather than a signal about the company.</p>')

    sold_section = "" if not sold else f"""
<h2>Sold</h2>
<p class="note">Closed when the verdict stopped being undervalued. The
return is locked at the exit price; the row stays for the record.</p>
<div class="table-scroll"><table><thead><tr>
  <th>Company</th><th>Entered</th><th>Sold</th>
  <th class="num">Return</th><th>Verdict at exit</th>
</tr></thead><tbody>{sold_rows}</tbody></table></div>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Portfolio — {esc(when)}</title>
<script>{PREPAINT_JS}</script>
<style>{CSS}</style>
</head><body>
<div class="wrap">

<header class="masthead">
  <h1>European stock screen</h1>
  <nav>{_nav_links('portfolio') if nav_links else ''}<button id="theme" type="button"
       title="Switch between light and dark"
       aria-label="Switch between light and dark">◐</button></nav>
</header>
<p class="runmeta">Portfolio marked {esc(when)}{f" · live since {esc(since)}"
  if since else ""} · {esc(marked.get("basis") or "price return")}</p>

<div class="summary-row">
  <div class="summary">
    {perf_head}
    <p class="lede {ov_cls if n else ''}">{lede}</p>
    <p class="lede-sub">{sub}</p>
    {figures}
  </div>
  {perf_col}
</div>

<h2>Held now</h2>
<div class="table-scroll"><table><thead><tr>
  <th>Company</th><th>Entered</th>{'' if public else
  '<th class="num">Entry</th><th class="num">Now</th>'}<th class="num">Since entry</th>
</tr></thead><tbody>{held_rows or
  f'<tr><td colspan="{3 if public else 5}" class="muted">Nothing held.</td></tr>'}</tbody></table></div>
{missing_note}
{void_note}
{sold_section}
{changed}

<footer>
  <p class="full">A position opens the day the screen first flags a company
  undervalued and closes when it no longer is. Returns are price only,
  exclude dividends, and use a fixed €100 per position with no benchmark
  comparison. Not investment advice.</p>
  {'<p class="full">Entry and current prices are omitted here — Yahoo '
   'Finance, the source, is not licensed for redistribution. Returns, '
   'and the Invested/Value figures above (a fixed €100 stake times a '
   'return ratio), are the model'"'"'s own output, not republished price '
   'data.</p>' if public else ''}
</footer>

</div>
<script>{THEME_JS}
{PERF_JS}</script>
</body></html>"""


SHEET_VIEWER_JS = """
(function(){
  var sel = document.getElementById('sheet-select');
  var out = document.getElementById('sheet-table');
  var status = document.getElementById('sheet-status');
  var wb = null;
  function show(name){
    var ws = wb.Sheets[name];
    out.innerHTML = ws ? XLSX.utils.sheet_to_html(ws, {editable: false}) : '';
  }
  fetch('valuation_template.xlsx').then(function(r){
    if (!r.ok) throw new Error(r.status);
    return r.arrayBuffer();
  }).then(function(buf){
    wb = XLSX.read(buf, {type: 'array'});
    wb.SheetNames.forEach(function(name){
      var opt = document.createElement('option');
      opt.value = name; opt.textContent = name;
      sel.appendChild(opt);
    });
    sel.value = wb.SheetNames.indexOf('Valuation') >= 0 ? 'Valuation' : wb.SheetNames[0];
    sel.disabled = false;
    status.remove();
    show(sel.value);
    sel.addEventListener('change', function(){ show(sel.value); });
  }).catch(function(){
    status.textContent = 'Could not load the workbook preview -- ' +
      'download the file below and open it directly instead.';
  });
})();"""


def build_methodology_page(nav_links: bool = True) -> str:
    """
    Static page previewing valuation_template.xlsx sheet-by-sheet via
    SheetJS. Safe to publish: the shipped template is the fictional
    DEMO.MI fixture (see tests/golden_test.py), not a real company or
    Yahoo Finance data.
    """
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Methodology</title>
<script>{PREPAINT_JS}</script>
<style>{CSS}</style>
</head><body>
<div class="wrap">

<header class="masthead">
  <h1>European stock screen</h1>
  <nav>{_nav_links('methodology') if nav_links else ''}<button id="theme" type="button"
       title="Switch between light and dark"
       aria-label="Switch between light and dark">◐</button></nav>
</header>

<p class="lede">How the valuation works.</p>
<p class="lede-sub">Every company is run through the same discounted
cash-flow model, using only figures taken directly from its published
accounts or from industry-, country- and market-wide parameters — no
company-specific judgement calls. The model itself is one Excel
workbook; below is that exact workbook, filled with a fictional demo
company, rendered live in your browser.</p>

<h2>The model, sheet by sheet</h2>
<p class="note">Fictional demo data (ticker DEMO.MI) — the same fixture
the project's own regression test checks against, not a real company
and not Yahoo Finance data. Pick a sheet to view its computed values.</p>
<div class="sheet-picker">
  <label for="sheet-select">Sheet</label>
  <select id="sheet-select" disabled><option>Loading…</option></select>
</div>
<p id="sheet-status" class="note muted">Loading valuation_template.xlsx…</p>
<div class="sheet-wrap"><div id="sheet-table"></div></div>
<p class="note"><a href="valuation_template.xlsx">Download the template
(.xlsx)</a> to open it in Excel or LibreOffice directly.</p>

<h2>Why five years, why this bar</h2>
<p class="note">Section from README, "Valuation and signal logic": fair
value is intrinsic value ± 25%; a company is flagged undervalued when
the market price sits more than 25% below that midpoint, and the
research queue on the Results page is exactly that filter applied
across the whole universe — no company is added or removed by hand.</p>

<footer>
  <p class="full">This page explains the mechanics of the model shown
  on the Results and Portfolio pages. Not investment advice.</p>
</footer>

</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js"></script>
<script>{THEME_JS}
{SHEET_VIEWER_JS}</script>
</body></html>"""


def main() -> int:
    p = argparse.ArgumentParser(
        description="Build the HTML report for a run.",
        parents=[config.common_args(db=False, out_dir=True, years=False)])
    p.add_argument("--run", type=Path, default=None)
    p.add_argument("--output", type=Path, default=None)
    p.add_argument("--public", action="store_true",
                   help="Also write a redacted report_<date>.public.html "
                        "with no Yahoo Finance-derived EUR price or "
                        "fair-value figures -- the only variant safe to "
                        "publish (see build_report.render()).")
    p.add_argument("--public-output", type=Path, default=None,
                   help="Where to write the public variant (default: "
                        "report_<date>.public.html next to --output).")
    p.add_argument("--open", action="store_true",
                   help="Open the page in your browser when it is written")
    args = p.parse_args()

    run_dir = args.run or latest_run(args.out_dir)
    if not run_dir or not run_dir.exists():
        print(f"No run found under {args.out_dir}.")
        print("Run:  python core/run_screen.py")
        return 1

    data = load(run_dir)
    if not data["companies"]:
        print(f"No provenance sidecars in {run_dir}.")
        return 1

    out = args.output or run_dir / f"report_{run_dir.name}.html"
    out.write_text(render(data), encoding="utf-8")
    queue = sum(1 for c in data["companies"] if c["flag"])
    print(f"{out}")
    print(f"  {len(data['companies'])} companies, {queue} in the queue")

    if args.public:
        public_out = (args.public_output or
                     out.with_name(out.stem + ".public.html"))
        public_out.write_text(render(data, public=True), encoding="utf-8")
        print(f"{public_out}  (no price/fair-value figures -- safe to publish)")

    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
