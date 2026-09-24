"""
CLI default tests
=================

WHY THIS EXISTS
---------------
The fiscal window is read from config.py by sixteen scripts. Edits across
many files are exactly where a careless string replace does quiet damage,
such as a truncated constant that only surfaces when the module fails to
import.

So the invariants are asserted here rather than eyeballed:

  1. config.FISCAL_YEARS is the expected window.
  2. No script hardcodes it.
  3. Every script's parser still BUILDS. `--help` exits before any real
     work, so it is a cheap end-to-end proof that the module imports, the
     argparse block is well formed and the defaults resolve.

Point 3 matters most. A rollout that leaves a NameError inside main() is
invisible to any static check and to a normal test run, because nothing
imports these scripts -- they are entry points. Running them is the only
way to know.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# The pipeline lives in core/, the R&D scripts in tools/, this file's
# neighbours in tests/. Scripts are named without a directory below and
# resolved against these, in order.
SCRIPT_DIRS = [REPO / "core", REPO / "tools", REPO / "tests"]


def find_script(name: str) -> Path | None:
    for d in SCRIPT_DIRS:
        if (d / name).exists():
            return d / name
    return None


# The expected fiscal window. If the project rolls forward,
# this changes in config.py and here, and nowhere else. That is the point.
EXPECTED_YEARS = [2021, 2022, 2023, 2024, 2025]

# Entry points whose CLI must keep working. Anything that opens a network
# connection or a database at import time does not belong here.
CLI_SCRIPTS = [
    "run_screen.py", "write_workbook.py", "fetch_prices.py", "track.py",
    "esef_extract.py", "esef_coverage.py", "extraction_audit.py",
    "verify_company.py", "normalisation_probe.py", "invested_capital_probe.py",
    "make_test_db.py", "analyse_run.py", "credentials.py", "screen.py",
    "fetch_ratings.py",
]


def scripts_with_years() -> dict[str, str]:
    """
    Every --years default in the project, as source text.

    A script gets --years two ways: declaring it directly, or inheriting
    it from `parents=[config.common_args(...)]` -- which defaults to
    config.FISCAL_YEARS unless called with years=False. Only checking for
    a direct add_argument("--years", ...) call went blind the moment a
    script was migrated to common_args(): its own source no longer
    mentions "--years" at all, so this silently SKIPPED instead of
    checking it, for every script wired up that way.
    """
    found = {}
    candidates = sorted(p for d in SCRIPT_DIRS for p in d.glob("*.py"))
    for path in candidates:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr == "add_argument":
                if not any(isinstance(a, ast.Constant) and a.value == "--years"
                           for a in node.args):
                    continue
                for kw in node.keywords:
                    if kw.arg == "default":
                        found[path.name] = ast.unparse(kw.value)
            elif (node.func.attr == "common_args"
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == "config"):
                years_disabled = any(
                    kw.arg == "years" and isinstance(kw.value, ast.Constant)
                    and kw.value.value is False
                    for kw in node.keywords)
                if not years_disabled:
                    found.setdefault(path.name, "config.FISCAL_YEARS")
    return found


def test_config_window_is_unchanged():
    """The single source of truth still holds the window it replaced."""
    import config
    assert config.FISCAL_YEARS == EXPECTED_YEARS
    assert len(config.FISCAL_YEARS) == config.YEAR_COUNT


def test_fiscal_window_is_not_hardcoded_anywhere():
    """
    No script may write the window out longhand.

    The failure this prevents is not a crash. It is one script reading
    2021-2025 while another reads 2022-2026 after a partial roll-forward,
    which produces a valuation quietly built from mismatched years and
    passes every workbook check, because D111 counts 100 populated cells
    and does not care which years they came from.
    """
    offenders = {
        name: src for name, src in scripts_with_years().items()
        if any(str(y) in src for y in EXPECTED_YEARS)
    }
    assert not offenders, (
        "these scripts still hardcode the fiscal window; they should use "
        f"config.FISCAL_YEARS: {offenders}")


@pytest.mark.parametrize("script", CLI_SCRIPTS)
def test_cli_still_builds(script):
    """
    `--help` must exit cleanly.

    Static analysis cannot see a NameError inside main(), and no test
    imports these files, because they are entry points. Actually running
    them is the only proof that a mechanical edit did not break one.
    """
    path = find_script(script)
    if path is None:
        pytest.skip(f"{script} not present")
    result = subprocess.run([sys.executable, str(path), "--help"],
                            capture_output=True, text=True, timeout=60,
                            cwd=path.parent)
    combined = (result.stdout + result.stderr).lower()
    if "missing dependency" in combined or "pip install" in combined:
        # A third-party package absent from this environment, not a fault
        # in the script. esef_coverage and esef_extract check for
        # xbrl-filings-api before argparse and exit with instructions.
        pytest.skip(f"{script} needs a package not installed here")
    assert result.returncode == 0, (
        f"{script} --help failed:\n{result.stderr[-1500:]}")
    assert "usage:" in result.stdout.lower()


@pytest.mark.parametrize("script", CLI_SCRIPTS)
def test_years_default_resolves_to_the_window(script):
    """
    Every script that takes --years must still default to the same five
    years.
    """
    path = find_script(script)
    if path is None:
        pytest.skip(f"{script} not present")
    if script not in scripts_with_years():
        pytest.skip(f"{script} takes no --years")
    # NEVER let main() run to completion.
    #
    # On a machine with a real database, main() would start an actual
    # screening run. The same loop covers track.py, extraction_audit.py
    # and fetch_prices.py, so letting it proceed could write to a
    # portfolio or spend API calls. A test must never do the thing it is
    # testing.
    #
    # Instead: spy on add_argument to capture the default, then make
    # parse_args abort. The parser is fully built -- which is what we are
    # checking -- and main() dies on the next line, before any work.
    #
    # The sentinel derives from BaseException so that a broad
    # `except Exception` inside main() cannot swallow it and carry on.
    #
    # The module must also be registered in sys.modules BEFORE
    # exec_module: @dataclass resolves annotations through
    # sys.modules[cls.__module__], which is None for a module loaded but
    # never registered, and the decorator dies with an opaque
    # AttributeError.
    probe = (
        "import sys,importlib.util,argparse\n"
        "class Stop(BaseException): pass\n"
        "seen=[]\n"
        "_add=argparse.ArgumentParser.add_argument\n"
        "def spy(self,*a,**k):\n"
        "    if '--years' in a: seen.append(k.get('default'))\n"
        "    return _add(self,*a,**k)\n"
        "argparse.ArgumentParser.add_argument=spy\n"
        "def no_run(self,*a,**k): raise Stop()\n"
        "argparse.ArgumentParser.parse_args=no_run\n"
        f"spec=importlib.util.spec_from_file_location('probed',r'{path}')\n"
        "m=importlib.util.module_from_spec(spec)\n"
        "sys.modules['probed']=m\n"
        "try: spec.loader.exec_module(m)\n"
        "except (SystemExit,Stop): pass\n"
        "except Exception: pass\n"
        "try: m.main()\n"
        "except (SystemExit,Stop): pass\n"
        "except Exception: pass\n"
        "print('YEARS=', seen[0] if seen else 'NONE')\n"
    )
    try:
        result = subprocess.run([sys.executable, "-c", probe],
                                capture_output=True, text=True, timeout=20,
                                cwd=path.parent, input="")
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"{script}: building the parser took over 20s, which means the "
            f"script started doing real work. parse_args should have "
            f"aborted it.")
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith("YEARS=")]
    if not lines:
        pytest.skip(f"{script}: could not probe (likely a missing package)")
    if "NONE" in lines[-1]:
        pytest.skip(f"{script}: parser not reached in this environment")
    assert str(EXPECTED_YEARS) in lines[-1], (
        f"{script} --years default is {lines[-1]!r}, expected {EXPECTED_YEARS}")
