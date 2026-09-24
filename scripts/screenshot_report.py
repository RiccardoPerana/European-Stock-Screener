#!/usr/bin/env python3
"""
Screenshot public/index.html for the README's hero image.

    python scripts/screenshot_report.py
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "public" / "index.html"
OUT = ROOT / "screenshots" / "presentation.png"


def main() -> int:
    if not SRC.exists():
        print(f"{SRC} not found -- run scripts/ci_screen.py first.")
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 900, "height": 450},
                                 device_scale_factor=2, color_scheme="dark")
        page.goto(SRC.resolve().as_uri())
        OUT.parent.mkdir(exist_ok=True)
        page.screenshot(path=str(OUT))
        browser.close()

    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
