#!/usr/bin/env python3
"""Draws the app icons (the web app manifest's, the Android app's and the iOS home-screen one) from one SVG mark:
ledger rules with a rising line, in the dashboard's graphite background and accent blue.

  .venv/bin/python dashboard/pwa/make_icons.py     # rewrites icon.svg and the PNGs next to this file

Rendered with Playwright's Chromium (the browser tests' one), so it needs nothing else installed.
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
BG, RULE, ACCENT = "#111110", "#3a3a37", "#5a9bef"
ART = (f'<path d="M120 176h272M120 256h272M120 336h272" stroke="{RULE}" stroke-width="12" stroke-linecap="round"/>'
       f'<path d="M120 352l88-88 64 48 120-136" fill="none" stroke="{ACCENT}" stroke-width="38" '
       'stroke-linecap="round" stroke-linejoin="round"/>'
       f'<circle cx="392" cy="176" r="30" fill="{ACCENT}"/>')


def svg(radius: int, scale: float) -> str:
    """radius: corner radius of the background (0 = full bleed, for maskable and iOS icons, which the OS crops);
    scale: the mark's size, so it stays inside a maskable icon's safe zone (the centre circle of 80% diameter)."""
    art = ART if scale == 1 else f'<g transform="translate(256 256) scale({scale}) translate(-256 -256)">{ART}</g>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
            f'<rect width="512" height="512" rx="{radius}" fill="{BG}"/>{art}</svg>\n')


ICONS = {  # file: (size px, corner radius, mark scale)
    "icon-192.png": (192, 116, 1),
    "icon-512.png": (512, 116, 1),
    "icon-maskable-512.png": (512, 0, 0.82),
    "apple-touch-icon.png": (180, 0, 0.9),
}


def main():
    from playwright.sync_api import sync_playwright
    with open(os.path.join(HERE, "icon.svg"), "w") as f:
        f.write(svg(116, 1))
    with sync_playwright() as p:
        b = p.chromium.launch()
        for name, (px, radius, scale) in ICONS.items():
            page = b.new_page(viewport={"width": px, "height": px})
            page.set_content(f'<style>html,body{{margin:0;background:transparent}}svg{{display:block;width:{px}px;'
                             f'height:{px}px}}</style>{svg(radius, scale)}')
            page.screenshot(path=os.path.join(HERE, name), omit_background=True)
            page.close()
        b.close()
    print("wrote icon.svg and", ", ".join(ICONS))


if __name__ == "__main__":
    main()
