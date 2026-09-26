#!/usr/bin/env python3
"""
Interface pages
===============

The dashboard and the shared page chrome, kept apart from app.py so the
server stays about routing and the markup stays readable.

THE DASHBOARD IS THE PIPELINE
-----------------------------
A count of companies ready to value raises the obvious question: where
does that number come from? Eight stages run before a valuation is
possible and each goes stale on its own schedule. Showing only some of
them cannot explain why the count is 170 rather than 400, and cannot warn
that the credit spread ladder is fourteen months old and about to put a
warning on every workbook.

So the dashboard reports every stage that needs attention, with what it
produced, when it last ran, and whether it is due -- read-only,
notifications and nothing else. Fixing anything happens on Settings, which
lists every stage next to the button that updates it, whether or not it is
already current. Splitting them means landing on the app always
answers "does anything need attention" before it answers "how do I
change it" -- the two questions do not compete for the same screen.

COLOUR
------
The academic-paper palette: Paper/off-white background,
Charcoal text and a single Muted Slate accent in light mode; Deep
Graphite, off-white text and Muted Steel in dark. That one accent
carries every ordinary interactive element -- buttons, links, focus --
so there is nothing to learn about what a second or third hue means,
with one deliberate exception: "Run everything" gets its own warm
accent, because it is the one button that touches the whole pipeline
rather than a single stage, not a general second accent in use
elsewhere. Health and verdict colours (the stage dots, stale warnings,
undervalued/overvalued) are a further, semantic layer on top: green/red carry a
specific meaning (safe vs. needs attention) that the base
palette does not specify, so they keep their own hues too.

Dark mode follows the system setting and swaps to the dark half of the
same token set rather than switching to a different design.
"""

from __future__ import annotations

import html

import config

CSS = """
:root{
  /* Paper #FBFBF9, Charcoal #222222, Muted Slate #4A5568. */
  --paper:#FBFBF9; --panel:#FFFFFF; --ink:#222222; --ink-soft:#5F5F5F;
  --rule:#DAD9D4; --rule-soft:#EDECE8; --field:#FFFFFF;
  --go:#4A5568; --go-hi:#3B4353;
  /* Health/verdict colours: a semantic layer the base palette
     does not specify, so they keep their own hues rather than folding
     into the one mandated accent -- see the COLOUR note above. */
  --ok:#2F6B4F; --warn:#9B4A3C; --warn-bg:#FBF4F2;
  --ok-bg:#F1F6F2; --idle:#9B9B95; --activity:#F0F0EC;
  /* "Run everything" only -- one button earns a colour of its own
     because it is the one action that touches the whole pipeline, not
     a single stage. A deliberate exception to "one accent" for exactly
     one button, not a second accent in general use. */
  --accent:#B5651D; --accent-hi:#8F4F16;
  /* One reading measure for the whole app, matching the results page. */
  --measure:66ch;
}
/* The system setting is the default; the toggle overrides it. Both use
   the same token names, so nothing downstream knows which is in force. */
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    /* Graphite #1E1E1E, off-white #E2E8F0, Muted Steel #64748B. */
    --paper:#1E1E1E; --panel:#282828; --ink:#E2E8F0; --ink-soft:#9BA3AE;
    --rule:#3A3A3A; --rule-soft:#2C2C2C; --field:#181818;
    --go:#64748B; --go-hi:#7C8AA0;
    --ok:#4E9B76; --warn:#D08878; --warn-bg:#2A1F1D;
    --ok-bg:#17251E; --idle:#6B6B66; --activity:#181818;
    --accent:#E0954F; --accent-hi:#F0AC72;
  }
}
:root[data-theme="dark"]{
  --paper:#1E1E1E; --panel:#282828; --ink:#E2E8F0; --ink-soft:#9BA3AE;
  --rule:#3A3A3A; --rule-soft:#2C2C2C; --field:#181818;
  --go:#64748B; --go-hi:#7C8AA0;
  --ok:#4E9B76; --warn:#D08878; --warn-bg:#2A1F1D;
  --ok-bg:#17251E; --idle:#6B6B66; --activity:#181818;
  --accent:#E0954F; --accent-hi:#F0AC72;
}
#theme{
  background:transparent; color:var(--ink-soft); border:0; padding:.2rem .4rem;
  cursor:pointer; font-size:.9375rem; line-height:1;
}
#theme:hover{color:var(--ink)}
.stored{color:var(--ok); font-size:.9375rem}
.flag code,.blurb code{font-size:.9em;
  background:var(--activity); padding:.05rem .3rem}
.stored::before{content:"✓ "}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-size:16px; line-height:1.55; font-variant-numeric:tabular-nums;
}
.wrap{max-width:960px; margin:0 auto; padding:2.5rem 1.5rem 6rem}
.masthead{display:flex; justify-content:space-between; align-items:baseline;
  gap:1rem; padding-bottom:.7rem; border-bottom:1px solid var(--ink);
  flex-wrap:wrap}
.masthead h1{font-size:1rem; font-weight:600; margin:0}
nav{display:flex; gap:1.25rem; font-size:.9375rem; align-items:baseline}
nav a{color:var(--ink-soft); text-decoration:none; padding-bottom:.15rem;
  border-bottom:1px solid transparent}
nav a:hover,nav a:focus-visible,nav a[aria-current]{
  color:var(--ink); border-bottom-color:var(--ink)}
/* Opening block, shared with the results and portfolio pages. */
.runmeta{color:var(--ink-soft); font-size:.875rem; margin:.9rem 0 0}
.lede{margin:3rem 0 .5rem; font-size:clamp(1.9rem,4.4vw,2.9rem);
  line-height:1.12; font-weight:600; max-width:20ch; letter-spacing:-.015em}
.lede-sub{color:var(--ink-soft); margin:0 0 3rem; max-width:var(--measure)}
h2{font-size:1.0625rem; font-weight:600; margin:4rem 0 .25rem}
.note{color:var(--ink-soft); font-size:.9375rem; max-width:var(--measure)}
h2 + .note{margin:0 0 1.25rem}
/* The About/Dashboard/Results/Portfolio/Settings blurbs on the
   Dashboard: plain reference prose, not the results/portfolio pages'
   narrow reading column, so let it use the whole width. */
.note.full{max-width:none}
.alerts{margin:1.75rem 0 0}
.alert{padding:.8rem 1rem; border-left:3px solid var(--warn);
  background:var(--warn-bg); font-size:.9375rem; margin-bottom:.5rem;
  display:flex; justify-content:space-between; gap:1rem; align-items:center;
  flex-wrap:wrap}
.alert.info{border-left-color:var(--idle); background:var(--rule-soft)}
.alert strong{font-weight:600}
.stage{display:flex; gap:1rem; align-items:flex-start; padding:1rem 0;
  border-bottom:1px solid var(--rule-soft); flex-wrap:wrap}
.stages{border-top:1px solid var(--ink)}
.dot{width:9px; height:9px; border-radius:50%; margin-top:.55rem; flex:none;
  background:var(--ok)}
.dot.stale{background:var(--warn)} .dot.missing{background:var(--idle)}
.dot.blocking{background:var(--warn); box-shadow:0 0 0 3px var(--warn-bg)}
.stage .body{flex:1; min-width:15rem}
.stage .title{font-weight:600}
.stage .count{color:var(--ink-soft); font-size:.9375rem}
.stage .blurb{color:var(--ink-soft); font-size:.875rem;
  max-width:var(--measure); margin-top:.15rem}
.stage .flag{color:var(--warn); font-size:.875rem; margin-top:.2rem}
.stage .blurb .warn{color:var(--warn)}
/* The button and the date stack on the right, right-aligned, centred as
   a block on the row so it sits level however tall the stage body
   grows -- the button on top, the date underneath, rather than side by
   side, which left no room for a wide flag next to a narrow button. */
.stage .status{display:flex; flex-direction:column; align-items:flex-end;
  gap:.35rem; align-self:center}
.stage .act{display:flex; gap:.5rem; align-items:center}
.when{color:var(--ink-soft); font-size:.8125rem; white-space:nowrap}
button{font:inherit; font-weight:600; color:var(--paper); background:var(--go);
  border:0; padding:.45rem 1rem; cursor:pointer; border-radius:2px}
button:hover:not(:disabled){background:var(--go-hi)}
button:disabled{background:var(--idle); cursor:not-allowed}
button.quiet{background:transparent; color:var(--ink);
  box-shadow:inset 0 0 0 1px var(--rule)}
button.quiet:hover:not(:disabled){background:var(--rule-soft)}
/* "Run everything" gets its own colour, deliberately breaking from
   every other button's shared accent: it is the one action that touches
   the whole pipeline rather than a single stage, and asked to look it. */
button.run-all{background:var(--accent); padding:1rem 2rem;
  line-height:1.3; text-align:center; font-weight:700}
button.run-all:hover:not(:disabled){background:var(--accent-hi)}
/* Every stage's action button the same fixed width, "Map industries"
   included: matching only that one button's width to its widest sibling
   ("Terminal") still left it wider than the common case ("Run" /
   "Update", both shorter). A single width applied to all of them, not
   a guess at one, is what actually makes them line up -- text-align
   centres the short ones, and white-space:normal lets the one long
   label wrap onto its own second line instead of forcing the button
   wide. */
.stage .act button{width:6.5rem; padding:.4rem .5rem; white-space:normal;
  line-height:1.25; text-align:center}
input[type=password],input[type=text]{font:inherit; padding:.45rem .6rem;
  border:1px solid var(--rule); background:var(--field); color:var(--ink);
  border-radius:2px; min-width:15rem}
.keyrow{display:flex; gap:.5rem; align-items:center; flex-wrap:wrap}
/* Live status. Hidden until a job starts, then moved directly beneath the
   step or alert whose button was pressed, so the output appears where the
   click was. `position:sticky` keeps it in view if the page is scrolled
   while the job runs. */
.statusbar{position:sticky; top:0; z-index:30;
  margin:.85rem -1.5rem 1.75rem; padding:.7rem 1.5rem .8rem;
  background:var(--paper); border-bottom:1px solid var(--rule-soft)}
.statusbar.live{border-bottom-color:var(--ok);
  box-shadow:0 7px 16px -10px rgba(0,0,0,.3)}
.bar{height:3px; background:var(--rule-soft); overflow:hidden}
.bar span{display:block; height:100%; background:var(--ink); width:0;
  transition:width .3s ease}
/* Jobs that cannot report a count (the ingestion scripts) still need to
   look alive. A sliding band says "working" without claiming a percentage. */
.bar.indeterminate span{width:40%; background:var(--ok);
  animation:slide 1.1s ease-in-out infinite}
@keyframes slide{0%{margin-left:-40%}100%{margin-left:100%}}
@media (prefers-reduced-motion:reduce){.bar.indeterminate span{
  animation:none; width:100%; opacity:.4}}
.statusbar .meta{display:flex; justify-content:space-between; gap:1rem;
  font-size:.875rem; color:var(--ink-soft); padding-top:.45rem}
.statusbar .meta #stage{color:var(--ink)}
.activity{margin-top:.6rem; max-height:12rem; overflow:auto; padding:.6rem .8rem;
  background:var(--activity); font-size:.8125rem; line-height:1.5;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  white-space:pre-wrap; overflow-wrap:anywhere; border-radius:2px}
.activity:empty{display:none}
.statusbar.live .activity:empty{display:block; min-height:2.25rem}
.assist{margin:1.5rem 0 0; padding:1rem 1.1rem; background:var(--panel);
  border:1px solid var(--rule-soft)}
.assist h3{font-size:.9375rem; font-weight:600; margin:0 0 .3rem}
.assist p{color:var(--ink-soft); font-size:.875rem; margin:0 0 .7rem;
  max-width:var(--measure)}
.assist textarea{width:100%; min-height:7rem; font:inherit;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.8125rem; padding:.6rem .7rem; border:1px solid var(--rule);
  background:var(--field); color:var(--ink); border-radius:2px; resize:vertical}
.assist .row{display:flex; gap:.5rem; align-items:center; flex-wrap:wrap;
  margin-top:.6rem}
.assist .msg{font-size:.8125rem; color:var(--ink-soft)}
.assist ul{margin:.6rem 0 0; padding-left:1.1rem; font-size:.8125rem;
  color:var(--ink-soft)}
.assist li{margin:.15rem 0}
/* The Dashboard's headline and "Run everything" share a row -- the
   button on the right of it, rather than its own row further down. The
   row carries .lede's own top margin; the headline's is zeroed so the
   two don't stack. align-items:center lines up the button's CENTRE with
   the headline's centre; translateY(50%) then drops the button by half
   of its own height, so it is the button's TOP edge that ends up level
   with the headline's centre, not its middle -- exact for any headline
   or button size, no fixed offset to keep in sync with font-size. */
.lede-row{display:flex; justify-content:space-between; align-items:center;
  gap:1.5rem; flex-wrap:wrap; margin:3rem 0 .5rem}
.lede-row .lede{margin:0}
.lede-row .run-all{transform:translateY(50%)}
footer{margin-top:5rem; padding-top:1.25rem; border-top:1px solid var(--rule);
  color:var(--ink-soft); font-size:.875rem}
footer p{margin:.4rem 0; max-width:var(--measure)}
/* The closing paragraph runs the full width of the page instead of the
   narrower reading column the rest of the footer keeps -- an explicit
   class on that one paragraph, not a :last-child guess. */
footer p.full{max-width:none}
a{color:inherit}
:focus-visible{outline:2px solid var(--ink); outline-offset:3px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
@media (max-width:620px){.wrap{padding:2rem 1.1rem 4rem}
  input[type=password],input[type=text]{min-width:0; width:100%}
  .statusbar{margin-left:-1.1rem; margin-right:-1.1rem;
    padding-left:1.1rem; padding-right:1.1rem}}
"""

# Keep-alive. Every page the app serves carries this: it tells the server
# the window is still open. When the pings stop -- tab closed, browser
# quit, machine asleep -- the server stops whatever job is running, so a
# closed window never leaves a scan going unattended. Bare JS (no <script>
# tags): dropped into shell()'s script block as {KEEPALIVE_JS}, and wrapped
# in its own <script> when appended to a served report.
KEEPALIVE_JS = """
(function(){
  function ping(){ try{ fetch('/api/ping', {cache:'no-store'})
    .catch(function(){}); }catch(e){} }
  ping();
  setInterval(ping, 3000);
  document.addEventListener('visibilitychange', function(){
    if(!document.hidden) ping();
  });
})();"""


def with_keepalive(html: str) -> str:
    """Append the keep-alive to a page the app did not render itself."""
    tag = f"<script>{KEEPALIVE_JS}</script>"
    lower = html.lower()
    i = lower.rfind("</body>")
    return html[:i] + tag + html[i:] if i != -1 else html + tag


# --------------------------------------------------------------------------
# Shared page chrome: the page nav and the light/dark toggle. Every page the
# app serves -- shell()-rendered or the standalone results report -- carries
# both, so there is always a way between pages and one theme control.
# --------------------------------------------------------------------------

_PAGES = (("/", "Dashboard"), ("/results", "Results"),
          ("/portfolio", "Portfolio"), ("/settings", "Settings"))


def _nav_links(current: str) -> str:
    return "".join(
        f'<a href="{href}"'
        + (' aria-current="page"' if current == href else "")
        + f'>{label}</a>'
        for href, label in _PAGES)


# Set data-theme before the stylesheet paints, so a saved dark choice never
# flashes white on the way in.
THEME_PREPAINT_JS = ("try{var t=localStorage.getItem('theme');"
                     "if(t)document.documentElement.setAttribute("
                     "'data-theme',t);}catch(e){}")

THEME_BUTTON = ('<button id="theme" type="button" '
                'title="Switch between light and dark" '
                'aria-label="Switch between light and dark">◐</button>')

# A plain two-way toggle: light <-> dark, and every click flips what is on
# screen. "Follow the system" was a third state, which made one click in
# three appear to do nothing whenever the OS theme already matched.
THEME_TOGGLE_JS = """
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

def results_page(report_html: str, current: str = "/results") -> str:
    """
    Add the page nav to a standalone build_report page (the results report
    or the portfolio) so its header matches every other page. That page
    already carries the identical masthead markup, its own copy of the
    theme toggle, and the dark palette (it must work opened straight from
    disk); the server only slots the page links into its <nav>, ahead of
    the theme button, and adds the keep-alive.
    """
    out = report_html
    if "<nav>" in out:
        out = out.replace("<nav>", "<nav>" + _nav_links(current), 1)
    return with_keepalive(out)


def shell(title: str, body: str, current: str = "", script: str = "") -> str:
    """Every app-rendered page: the same head, nav, and theme control."""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{title}</title>
<script>{THEME_PREPAINT_JS}</script>
<style>{CSS}</style>
</head><body>
<div class="wrap">
<header class="masthead">
  <h1>European stock screen</h1>
  <nav>{_nav_links(current)}{THEME_BUTTON}</nav>
</header>
{body}
</div>
<script>
{THEME_TOGGLE_JS}
{KEEPALIVE_JS}
{script}</script>
</body></html>"""


def placeholder(title: str, message: str) -> str:
    """Emptiness is a moment for direction, not an apology."""
    return shell(title, f"""
<h2>{title}</h2>
<p class="note">{message}</p>
<p><a href="/">Back to the dashboard</a></p>""")


# Starts/stops jobs and polls their progress into a statusbar. Settings
# uses the whole thing (every stage's button, the industry-mapping and
# API-key panels); the Dashboard loads it too, but only to run the one
# "Run everything" button -- it has no other [data-job]/[data-assist]
# elements, so the rest of this simply never matches anything there.
PIPELINE_JS = """
const $ = s => document.querySelector(s);
let last = 'idle';
let sawRunning = false;   // reset by the post-job page reload

const JOB_LABELS = {
  all:'Running the full pipeline', screen:'Valuing companies',
  prices:'Updating prices', coverage:'Finding the filings',
  extract:'Reading the statements', identity:'Matching listings',
  shares:'Counting shares', parameters:'Refreshing parameters'
};

async function post(path, data) {
  const r = await fetch(path, {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(data||{})});
  return {ok:r.ok, body: await r.json()};
}

// Move the status/activity panel to sit right after `anchor` (the .stage
// row or .alert whose button was pressed), or back to its home slot when
// anchor is null.
function moveStatus(anchor) {
  (anchor || $('#sb-home')).insertAdjacentElement('afterend', $('#statusbar'));
}

async function refresh() {
  let s;
  try { s = await (await fetch('/api/status')).json(); } catch (e) { return; }
  const t = s.task, running = t.status === 'running';
  if (running) sawRunning = true;

  document.querySelectorAll('[data-job],[data-assist],#ind-gemini,#ind-copy,'
    + '#ind-apply,#tables-check,#tables-apply').forEach(b => b.disabled = running);
  $('#stop').hidden = !running;

  // A job that reports a total drives a real bar; the ingestion scripts
  // report none, so show a moving band rather than a bar stuck at 0%.
  const determinate = !running || t.total > 0;
  const bar = document.querySelector('.bar');
  bar.classList.toggle('indeterminate', running && !determinate);
  $('#bar').style.width = determinate
    ? (running ? t.percent : (t.status === 'done' ? 100 : 0)) + '%' : '';

  $('#stage').textContent = running
    ? (JOB_LABELS[t.name] || ('Running ' + t.name))
      + (t.detail ? ' — ' + t.detail : '')
    : ({done:'Finished', failed:'Stopped with an error',
        cancelled:'Stopped', idle:'Nothing running'}[t.status] || '');
  $('#count').textContent = t.total ? t.done + ' of ' + t.total : '';
  $('#activity').textContent = (t.log || []).join('\\n')
    + (t.error ? '\\n\\n' + t.error : '');
  // Keep the activity scrolled to the newest line while a job runs.
  if (running) { const a = $('#activity'); a.scrollTop = a.scrollHeight; }

  // Show the panel while a job runs or has failed, and briefly after a job
  // we watched finish (until the reload below). A stale done/failed state
  // found after a fresh page load stays hidden.
  const sb = $('#statusbar');
  const justFinished = sawRunning
    && (t.status === 'done' || t.status === 'cancelled');
  const showBar = running || t.status === 'failed' || justFinished;
  sb.hidden = !showBar;
  sb.classList.toggle('live', running || t.status === 'failed');

  if (showBar && running && sb.dataset.job !== t.name) {
    // A job is running but the panel is not under its control -- the page
    // was reloaded mid-job, or something else started it. Re-anchor it.
    const btn = document.querySelector('.stages [data-job="' + t.name + '"]')
             || document.querySelector('.alert [data-job="' + t.name + '"]');
    moveStatus(btn ? btn.closest('.stage, .alert') : null);
    sb.dataset.job = t.name;
  } else if (!showBar && sb.dataset.job) {
    moveStatus(null);
    sb.dataset.job = '';
  }

  // Reload to refresh the stage counts once a run finishes -- but not on
  // failure, where the error must stay on screen to be read.
  if (last === 'running' && !running && t.status !== 'failed') {
    setTimeout(() => location.reload(), 2500);
  }
  last = t.status;
}

document.addEventListener('click', async e => {
  const job = e.target.dataset && e.target.dataset.job;
  if (job) {
    const r = await post('/api/run', {job});
    if (!r.ok) { alert(r.body.error || 'Could not start.'); return; }
    // Drop the panel in right under the step / alert that was clicked and
    // bring it into view, so its output appears where the click happened.
    const sb = $('#statusbar');
    moveStatus(e.target.closest('.stage, .alert'));
    sb.dataset.job = job;
    sb.hidden = false;
    sb.scrollIntoView({behavior: 'smooth', block: 'center'});
    refresh();
  }
  const panel = e.target.dataset && e.target.dataset.assist;
  if (panel) {
    const p = $('#assist-' + panel);
    if (p) { p.hidden = !p.hidden;
      if (!p.hidden) p.scrollIntoView({behavior:'smooth', block:'center'}); }
  }
  if (e.target.id === 'ind-gemini') {
    const msg = $('#ind-gemini-msg');
    msg.textContent = 'Asking Gemini…';
    const r = await post('/api/industry-classify-gemini');
    if (!r.ok) { msg.textContent = r.body.error || 'Failed.'; return; }
    const b = r.body;
    msg.textContent = b.log === 'Nothing to classify.' ? b.log :
      'stored ' + b.stored + ', rejected ' + b.rejected
      + ', unknown ' + b.unknown + ', ' + b.still_unmapped + ' still unmapped';
    if (b.stored || b.rejected) setTimeout(() => location.reload(), 1800);
  }
  if (e.target.id === 'ind-copy') {
    const msg = $('#ind-copy-msg'); msg.textContent = 'Loading…';
    try {
      const r = await (await fetch('/api/industry-prompt')).json();
      if (!r.count) { msg.textContent =
        'Nothing to map — every company already has an industry.'; return; }
      const box = $('#ind-prompt'); box.value = r.prompt; box.hidden = false;
      try { await navigator.clipboard.writeText(r.prompt);
        msg.textContent = r.count +
          ' companies copied to the clipboard. Paste into any LLM.'; }
      catch (err) { box.focus(); box.select();
        msg.textContent = r.count +
          ' companies ready — the clipboard was blocked, so select and '
          + 'copy the box below.'; }
    } catch (err) { msg.textContent = 'Could not load the prompt: ' + err; }
  }
  if (e.target.id === 'ind-apply') {
    const text = ($('#ind-reply').value || '').trim();
    const msg = $('#ind-apply-msg');
    if (!text) { msg.textContent = 'Paste the model\\'s reply first.'; return; }
    msg.textContent = 'Applying…';
    const r = await post('/api/industry-map', {text});
    if (!r.ok) { msg.textContent = r.body.error || 'Failed.'; return; }
    const b = r.body;
    msg.textContent = 'stored ' + b.stored + ', rejected ' + b.rejected
      + ', unknown ' + b.unknown + ', ' + b.still_unmapped + ' still unmapped';
    setTimeout(() => location.reload(), 1800);
  }
  if (e.target.id === 'tables-check') {
    const msg = $('#tables-check-msg'), diffBox = $('#tables-diff'),
          applyRow = $('#tables-apply-row');
    msg.textContent = 'Checking…'; diffBox.textContent = '';
    applyRow.hidden = true;
    try {
      const r = (await post('/api/tables-check')).body;
      if (r.error) { msg.textContent = r.error; return; }
      if (!r.changes.length) {
        // Nothing moved, but applying still re-stamps the vintage as checked
        // today, which keeps D122's 14-month warning from firing.
        msg.textContent = "Matches Damodaran's site"
          + (r.vintage_now ? ' (vintage ' + r.vintage_now + ')' : '')
          + '. Apply to mark it as checked today.';
        applyRow.hidden = false;
        return;
      }
      msg.textContent = r.changes.length
        + ' row(s) differ from refdata_tables.json:';
      const ul = document.createElement('ul');
      r.changes.forEach(c => {
        const li = document.createElement('li');
        li.textContent = c.text;
        ul.appendChild(li);
      });
      diffBox.appendChild(ul);
      applyRow.hidden = false;
    } catch (err) { msg.textContent = 'Could not check: ' + err; }
  }
  if (e.target.id === 'tables-apply') {
    const msg = $('#tables-apply-msg');
    msg.textContent = 'Applying…';
    const r = await post('/api/tables-apply', {});
    if (!r.ok) { msg.textContent = r.body.error || 'Failed.'; return; }
    msg.textContent = 'Applied — new vintage ' + r.body.vintage + '.';
    $('#tables-apply-row').hidden = true;
    setTimeout(() => location.reload(), 1800);
  }
  if (e.target.id === 'stop') {
    await post('/api/cancel');
    refresh();
  }
  const forget = e.target.dataset && e.target.dataset.forget;
  if (forget) {
    if (!confirm('Remove the saved key? You will have to paste it again.')) return;
    const r = await post('/api/key', {provider: forget, key: ''});
    if (!r.ok) { alert(r.body.error); return; }
    location.reload();
  }
  const save = e.target.dataset && e.target.dataset.save;
  if (save) {
    const input = document.querySelector('[data-key="' + save + '"]');
    const r = await post('/api/key', {provider: save, key: input.value});
    if (!r.ok) { alert(r.body.error); return; }
    input.value = ''; location.reload();
  }
});
refresh();
setInterval(refresh, 1200);
"""

# The status/activity panel markup, identical on the Dashboard and Settings
# -- PIPELINE_JS moves it under whichever row was clicked on either page, so
# both need the same elements in place, just hidden, before any job starts.
STATUSBAR = """
<span id="sb-home" hidden></span>
<div class="statusbar" id="statusbar" hidden>
  <div class="bar"><span id="bar"></span></div>
  <div class="meta">
    <span id="stage">Nothing running</span>
    <span id="count"></span>
  </div>
  <button id="stop" class="quiet" hidden style="margin-top:.5rem">Stop</button>
  <div class="activity" id="activity"></div>
</div>"""


def _when_text(s: dict) -> str:
    if s["age_days"] is not None:
        return "today" if s["age_days"] == 0 else f"{s['age_days']} days ago"
    return s["date"] or "never"


def _stage_row(s: dict) -> str:
    """The Settings version: the same row, plus whatever updates it."""
    when = _when_text(s)
    flag = (f'<div class="flag">{s["message"]}</div>' if s["message"] else "")
    if s["key"] in ASSIST:
        # Reveals an in-page panel instead of starting a job.
        button = (f'<button data-assist="{s["key"]}">{ASSIST[s["key"]]}</button>')
        if not s["done"]:
            flag = flag or f'<div class="flag">{ASSIST_NOTE[s["key"]]}</div>'
    else:
        button = (f'<button data-job="{s["job"]}">'
                  f'{"Update" if s["done"] else "Run"}</button>')
    return f"""
<div class="stage">
  <span class="dot {s['health']}" aria-hidden="true"></span>
  <div class="body">
    <div class="title">{s['title']}
      <span class="count">— {s['count'] if s['count'] else ''} {s['unit']}</span></div>
    <div class="blurb">{s['blurb']}</div>
    {flag}
  </div>
  <div class="status">
    <span class="act">{button}</span>
    <span class="when">{when}</span>
  </div>
</div>"""


# Stages driven by an in-page panel rather than a one-press job.
# Assigning industries has a person in the middle by design -- it copies a
# prompt for a language model and takes the reply back. The credit ladder
# is fetched by fetch_ratings.py, but the two source pages are fragile
# enough -- one has an unrelated second table sitting right next to the
# one this project uses -- that a person looks at the diff before it is
# written, so it is a panel rather than a plain Update button.
ASSIST = {"industries": "Map industries", "tables": "Check for update"}
ASSIST_NOTE = {
    "industries": "Use the <strong>industry mapping</strong> panel below: "
                  "copy the prompt, paste it into any LLM, paste the reply "
                  "back.",
    "tables": "Use the <strong>credit spread ladder</strong> panel below: "
             "check Damodaran's tables, then apply if anything changed.",
}


def _price_watch_block(hits: list[dict]) -> str:
    """
    'Crossed a trigger since the last scan' notice, separate from the
    stage alerts above it -- this is not a pipeline problem to fix, it
    is a heads-up, so it does not count toward "N steps need attention"
    or get the "fix one at a time from Settings" line, neither of which
    apply to it.
    """
    crossed = [h for h in hits if h["status"] == "crossed"]
    approaching = [h for h in hits if h["status"] == "approaching"]
    if not (crossed or approaching):
        return ""

    def names(rows):
        shown = ", ".join(html.escape(r["ticker"]) for r in rows[:6])
        rest = len(rows) - 6
        return shown + (f" (+{rest} more)" if rest > 0 else "")

    parts = []
    if crossed:
        parts.append(f"<strong>{len(crossed)}</strong> now trade below "
                     f"their last-quarter trigger price: {names(crossed)}")
    if approaching:
        parts.append(f"<strong>{len(approaching)}</strong> within the "
                     f"watch margin: {names(approaching)}")
    return (f'<div class="alerts"><div class="alert info"><span>'
           f'<strong>Price watch</strong> {" — ".join(parts)}. Since the '
           f'last full scan; worth a look before the next one.'
           f'</span></div></div>')


def dashboard(steps: list[dict], alerts: list[dict],
             task: dict | None = None,
             price_watch: list[dict] | None = None) -> str:
    """
    Notifications, and only the ones that need attention -- a stage that
    is current is not listed. The one control on the page is "Run
    everything", for fixing all of them in one go; updating a single
    stage, including one that is already current, is what Settings is
    for, which also lists every stage individually.

    Kept short on purpose: the button's own explanation sits beside it
    rather than below the fold, and the "About this program" paragraph
    (the program, plus what each of the other three pages is for) is
    behind a <details> disclosure, closed by default -- a first-time
    reader can still find it; a returning one is not made to read past
    it to see whether anything needs doing.
    """
    alert_html = "".join(
        f"""<div class="alert{' info' if a['health'] == 'missing' else ''}">
          <span><strong>{a['title']}</strong> {a['message']}</span>
        </div>""" for a in alerts)

    task = task or {}
    task_note = ""
    if task.get("status") == "running":
        task_note = (
            '<div class="alert info"><span>A job is currently running: '
            f'<strong>{html.escape(str(task.get("name") or ""))}</strong>. '
            'Watch it or stop it from '
            '<a href="/settings">Settings</a>.</span></div>')
    elif task.get("status") == "failed":
        task_note = (
            '<div class="alert"><span>A job failed: '
            f'<strong>{html.escape(str(task.get("name") or ""))}</strong>. '
            'See <a href="/settings">Settings</a> for the error.'
            '</span></div>')

    by_key = {s["key"]: s for s in steps}
    ready = by_key.get("screen", {}).get("count") or 0
    last_scan = by_key.get("screen", {}).get("date")
    fy = config.FISCAL_YEARS
    runmeta = (f"{'Last scan ' + last_scan if last_scan else 'No scan yet'} · "
               f"{ready} ready to value · financial years {fy[0]}–{fy[-1]}")
    n = len(alerts)
    headline = ("Everything is up to date." if not n
                else "One step needs attention." if n == 1
                else f"{n} steps need attention.")

    body = f"""
<p class="runmeta">{runmeta}</p>
<div class="lede-row">
  <p class="lede">{headline}</p>
  <button data-job="all" class="run-all">Run<br>Everything</button>
</div>
<p class="lede-sub">Each stage feeds the next. A company can only be valued
once it has five complete years of accounts, a listing, a price, a share
count and an industry — and each of those goes stale on its own schedule.</p>
{STATUSBAR}

{f'<div class="alerts">{task_note}{alert_html}</div>'
 if (alerts or task_note) else ''}
{'<p class="note">Or fix one at a time from <a href="/settings">Settings'
 '</a>.</p>' if alerts else ''}
{_price_watch_block(price_watch or [])}

<h2>About this program</h2>
<p class="note full">An automated screen for European listed companies:
it pulls financial statements from the ESEF filing archive, prices from
Yahoo Finance, and risk parameters from Damodaran's datasets and the
ECB, writes each company into an Excel discounted-cash-flow model, reads
the verdict back out, and tracks the picks over time. Everything runs on
this computer; nothing is published anywhere.</p>

<footer>
  <p>Everything runs on this computer. The browser is only the display.</p>
  <p class="full">This page shows what needs attention across the pipeline
  and nothing else; run or refresh anything from Settings.</p>
</footer>"""
    return shell("Stock screen", body, current="/", script=PIPELINE_JS)


def settings(steps: list[dict], keys: dict, engine: dict | None = None) -> str:
    """
    Everything that updates part of the pipeline: every stage's button
    -- including stages already up to date, so a refresh is never
    blocked on staleness -- the industry-mapping panel, and API keys.
    What needs attention lives on the Dashboard instead of here.

    `engine` (recalc_engine()'s result) surfaces a missing spreadsheet
    engine here too, since the Zero Terminal Policy rules out a startup
    print() as the only place to see it. Silent when one is found;
    nothing to tell a working setup it is working.
    """
    key_rows = "".join(f"""
  <div class="stage">
    <span class="dot {'ok' if k['set'] else 'missing'}" aria-hidden="true"></span>
    <div class="body">
      <div class="title">{k['label']}</div>
      <div class="blurb">{k['note']} — {k['where']}.
        <a href="{k['signup']}" target="_blank" rel="noopener">Get a key</a></div>
    </div>
    <span class="keyrow">{
      # A stored key is never shown again, so a password box sitting there
      # cannot say whether one exists. Showing the field only when there
      # is nothing saved, and a Remove button when there is, makes the
      # state readable at a glance instead of guessed.
      f'''<span class="stored">Saved</span>
      <button class="quiet" data-forget="{name}">Remove</button>'''
      if k["set"] else
      f'''<input type="password" data-key="{name}" placeholder="Paste a key"
             autocomplete="off" aria-label="{k['label']} key">
      <button class="quiet" data-save="{name}">Save</button>'''
    }</span>
  </div>""" for name, k in keys.items())

    engine_note = ("" if not engine or engine.get("ok") else
                  f'<div class="alerts"><div class="alert">'
                  f'<span>{html.escape(engine["message"])}</span></div>'
                  f'</div>')

    body = f"""
<p class="lede">Update the pipeline</p>
<p class="lede-sub">Run or refresh any stage below, whether or not it is
already current. What needs attention is on the
<a href="/">Dashboard</a>.</p>
{engine_note}
{STATUSBAR}

<h2>The pipeline</h2>
<div class="stages">{''.join(_stage_row(s)
                              for s in steps)}</div>

<div class="assist" id="assist-industries" hidden>
  <h3>Industry mapping</h3>
  <p>Every listed company needs one of Damodaran's industries before it can
  be valued, and no free source carries a usable one. It is safe to do a
  batch at a time.</p>
  <div class="row">
    <button id="ind-gemini">Classify with Gemini</button>
    <span class="msg" id="ind-gemini-msg"></span>
  </div>
  <p class="lede-sub" style="margin:.6rem 0 1rem">Needs a Gemini key saved
  below. Or do it by hand instead: copy the prompt into any language model,
  then paste its reply back in step 2.</p>
  <div class="row">
    <button id="ind-copy" class="quiet">Copy AI prompt</button>
    <span class="msg" id="ind-copy-msg"></span>
  </div>
  <textarea id="ind-prompt" readonly hidden
            aria-label="Generated prompt"></textarea>
  <p style="margin:1rem 0 .2rem">Paste the model's reply
  (<code>TICKER | Industry</code>, one per line):</p>
  <textarea id="ind-reply"
            placeholder="BRE | Auto Parts&#10;AVIO | Aerospace/Defense"></textarea>
  <div class="row">
    <button id="ind-apply">Apply result</button>
    <span class="msg" id="ind-apply-msg"></span>
  </div>
</div>

<div class="assist" id="assist-tables" hidden>
  <h3>Credit spread ladder</h3>
  <p>Damodaran's large- and small-cap synthetic-rating tables, fetched
  fresh from his site and compared with what is currently in
  <code>refdata_tables.json</code>. His page for the large-cap table
  carries a second, unrelated ladder right next to the one this project
  uses, so nothing is written until you check the result below and apply
  it. Applying an unchanged table marks it as checked today.</p>
  <div class="row">
    <button id="tables-check" class="quiet">Check Damodaran's tables</button>
    <span class="msg" id="tables-check-msg"></span>
  </div>
  <div id="tables-diff"></div>
  <div class="row" id="tables-apply-row" hidden>
    <button id="tables-apply">Apply</button>
    <span class="msg" id="tables-apply-msg"></span>
  </div>
</div>

<h2>Data sources</h2>
<p class="note">Keys are saved to your operating system's credential store,
never to a file in this folder, and are not shown again once saved. Prices
come from Yahoo and need no key.</p>
<div class="stages">{key_rows}</div>

<footer>
  <p>Everything runs on this computer. The browser is only the display.</p>
  <p class="full">This page runs or refreshes any pipeline stage, including
  ones already current, plus industry mapping and API keys.</p>
</footer>"""
    return shell("Settings", body, current="/settings", script=PIPELINE_JS)
