# PyInstaller build spec -- StockScreen.exe
# ==========================================
#
# Builds a single portable .exe: no installer, no registry entries, no
# admin rights needed. Normally run through `python packaging/build_dist.py`,
# which also copies the data files beside the .exe; by hand, from the
# repository root:
#
#     pyinstaller packaging/app.spec --distpath packaging/dist --workpath packaging/build
#
# WHY ONEFILE, AND WHY NOTHING IS EMBEDDED AS "DATA"
# ----------------------------------------------------
# onefile mode extracts everything to a fresh temp folder (sys._MEIPASS)
# on every launch and deletes it on exit -- fine for the app's own code,
# fatal for anything meant to persist. So nothing the app WRITES
# (financials.db, parameters.json, portfolio.db, valuations/, ...) is
# bundled here at all: core/config.py points ROOT at sys.executable's own
# directory in a frozen build, so all of that lives next to the .exe on
# real, permanent disk instead.
#
# valuation_template.xlsx is read-only but still isn't embedded, for the
# same reason kept simple: build_dist.py (see that file) copies it next
# to the built .exe after PyInstaller finishes, so config.TEMPLATE_PATH
# (ROOT / "valuation_template.xlsx") finds it exactly where every other
# data file lives. One rule for where files are, not two.
#
# HIDDEN IMPORTS
# --------------
# gui/app.py's SCRIPT_JOBS dispatches to esef_coverage / esef_extract /
# resolve_identity / fetch_parameters via importlib.import_module(name) --
# a computed string PyInstaller's static analysis cannot see. Left out,
# the button for any of those stages would fail at runtime with a
# ModuleNotFoundError and, since this is a --noconsole build, no visible
# error at all. They are listed explicitly below instead of hoped for.

import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH).resolve().parent
CORE = ROOT / "core"
GUI = ROOT / "gui"

a = Analysis(
    [str(GUI / "app.py")],
    pathex=[str(CORE), str(GUI)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "esef_coverage", "esef_extract", "resolve_identity",
        "fetch_parameters",
        # win32com's COM dispatch and keyring's OS-backend discovery both
        # resolve plugins at runtime rather than via a plain `import`;
        # PyInstaller's static analysis misses that pattern too.
        "win32com", "win32com.client", "win32timezone",
        "keyring.backends.Windows",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="StockScreen",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Zero Terminal Policy: no console window, ever.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
