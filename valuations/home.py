"""Home market value: ComeHome AVM + county appraisal district (CAD) market value, blended.

Sources (free, no account, verified 2026-10-21):
  1. ComeHome by HouseCanary AVM (comehome.com/property-details/<slug>), best effort. Its GraphQL API
     sits behind AWS WAF, so one headless Chromium visit a month searches the address and reads the
     `avm` block (priceMean / priceLower / priceUpper / fsd / quality) from the page's own responses.
     After a few visits in a row WAF escalates to a "confirm you are human" CAPTCHA; we never solve it,
     the run just falls back to the CAD value alone (`detail.errors.comehome`).
  2. The county appraisal district's market value for the property (TrueProdigy public API, the one the
     <county>.prodigycad.com sites use). Valued as of Jan 1 of the tax year; a protested value is better
     read as a conservative floor-ish anchor than a sale price.
Zillow, Redfin, Realtor.com, Trulia, Homes.com and Movoto all return 403/429 (bot walls) from a home
connection, so they are not used. RentCast's free tier (50 calls/mo) would add a third AVM but needs a sign-up.

Value = median of the available estimates (mean when there are two). If they differ by more than 15%,
`detail.disagreement` says so; the median is still the conservative middle.

The address, ComeHome slug, CAD office/host/property id and the balance-sheet label come from
personal.json "home" (see personal.example.json).
"""
from __future__ import annotations

import os
import statistics
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import personal  # noqa: E402

_CFG = personal.section("home")
NAME = "home"
EVERY_DAYS = 30
ASSET = personal.require("home", "asset")
ASSETS = (ASSET,)

ADDRESS = personal.require("home", "address")
SEARCH_TEXT = _CFG.get("search_text") or ADDRESS            # typed into ComeHome's search box
SUGGESTION_TEXT = _CFG.get("suggestion_text") or ADDRESS.split(",")[0]  # the suggestion to click
COMEHOME_SLUG = personal.require("home", "comehome_slug")
COMEHOME_URL = f"https://www.comehome.com/property-details/{COMEHOME_SLUG}"
CAD_OFFICE = personal.require("home", "cad_office")         # TrueProdigy office name, e.g. the county
CAD_HOST = personal.require("home", "cad_host")             # e.g. <county>.prodigycad.com
CAD_PID = str(personal.require("home", "cad_pid"))
CAD_NOTE = _CFG.get("cad_note") or "appraisal-district market value as of Jan 1"
CAD_URL = f"https://{CAD_HOST}/property-detail/{CAD_PID}"
CAD_API = "https://prod-container.trueprodigyapi.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"


def comehome() -> dict:
    from playwright.sync_api import sync_playwright

    found: dict = {}

    def on_resp(r):
        if "property-graph.comehome.com/graphql" not in r.url:
            return
        try:
            pl = (r.json().get("data") or {}).get("propertyLookup") or {}
        except Exception:
            return
        avm = pl.get("avm") or {}
        if avm.get("priceMean") and len(avm) >= len(found.get("avm", {})):  # keep the fullest avm block
            found["avm"] = avm
        for k in ("livingSpace", "structure", "site"):
            if pl.get(k):
                found[k] = pl[k]

    # A cold deep link to the property page stalls on the WAF challenge (HTTP 202/405), so do what a
    # visitor does: open the homepage, search the address, pick the suggestion (client-side navigation).
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}  # headless only, never the desktop
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, env=env)
        try:
            pg = b.new_page(user_agent=UA, viewport={"width": 1280, "height": 900})
            pg.on("response", on_resp)
            pg.goto("https://www.comehome.com/", wait_until="domcontentloaded", timeout=60000)
            for _ in range(8):  # the invisible WAF challenge reloads itself into the real page
                pg.wait_for_timeout(1000)
                if "human" in pg.title().lower() and pg.get_by_text("confirm you are human").count():
                    # an interactive CAPTCHA: never try to solve it, just skip ComeHome this run
                    raise RuntimeError("ComeHome showed a human-verification CAPTCHA; skipped")
                if pg.locator("input").count():
                    break
            box = pg.locator("input").first
            box.wait_for(timeout=30000)
            box.click()
            box.type(SEARCH_TEXT, delay=60)
            sugg = pg.get_by_text(SUGGESTION_TEXT, exact=False).first
            try:
                sugg.wait_for(timeout=15000)
                sugg.click()
            except Exception:
                pg.keyboard.press("ArrowDown")
                pg.keyboard.press("Enter")
            for _ in range(45):
                if found.get("avm", {}).get("priceUpper"):
                    break
                pg.wait_for_timeout(1000)
            found["page_url"] = pg.url
        finally:
            b.close()
    if not found.get("avm"):
        raise RuntimeError("ComeHome page loaded but returned no AVM")
    return found


def cad() -> dict:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Origin": f"https://{CAD_HOST}", "Referer": f"https://{CAD_HOST}/"})
    tok = s.post(f"{CAD_API}/trueprodigy/cadpublic/auth/token", json={"office": CAD_OFFICE},
                 timeout=30).json()["user"]["token"]
    r = s.post(f"{CAD_API}/public/property/search", json={"pid": {"operator": "=", "value": CAD_PID}},
               headers={"Authorization": tok}, timeout=30)
    r.raise_for_status()
    years = [x for x in r.json()["results"] if isinstance(x.get("marketValue"), (int, float)) and x["marketValue"] > 0]
    if not years:
        raise RuntimeError("CAD returned no market values")
    latest = max(years, key=lambda x: int(x["pYear"]))
    return {"year": int(latest["pYear"]), "market": float(latest["marketValue"]),
            "land": latest.get("landValue"), "improvement": latest.get("improvementValue"),
            "owner": latest.get("name"),
            "history": {x["pYear"]: x["marketValue"] for x in sorted(years, key=lambda x: x["pYear"])}}


def fetch() -> list[dict]:
    estimates, detail, errors = {}, {"address": ADDRESS}, {}
    try:
        ch = comehome()
        avm = ch["avm"]
        estimates["comehome_avm"] = float(avm["priceMean"])
        detail["comehome"] = {"url": COMEHOME_URL, "provider": "HouseCanary AVM via ComeHome", **avm,
                              "living_space": ch.get("livingSpace"), "structure": ch.get("structure")}
    except Exception as e:
        errors["comehome"] = str(e)[:300]
    try:
        t = cad()
        estimates[f"cad_{t['year']}_market"] = t["market"]
        detail["cad"] = {"url": CAD_URL, "property_id": CAD_PID, **t, "note": CAD_NOTE}
    except Exception as e:
        errors["cad"] = str(e)[:300]
    if not estimates:
        raise RuntimeError(f"no home estimate available: {errors}")

    vals = sorted(estimates.values())
    value = round(statistics.median(vals), -2)
    spread = vals[-1] / vals[0] - 1
    detail.update({
        "estimates": estimates,
        "method": "median of available estimates (ComeHome/HouseCanary AVM, appraisal-district market value), rounded to $100",
        "spread_pct": round(100 * spread, 1),
        "sources": [COMEHOME_URL, CAD_URL],
        "not_used": "Zillow/Redfin/Realtor/Trulia/Homes.com/Movoto block automated requests (403/429)",
    })
    if spread > 0.15:
        detail["disagreement"] = f"sources differ by {spread:.0%}; median used as conservative middle"
    if errors:
        detail["errors"] = errors
    if len(estimates) == 1:
        detail["warning"] = "only one source available this run"
    return [{"asset": ASSET, "value": value, "source": "+".join(estimates), "detail": detail}]


if __name__ == "__main__":
    import json
    print(json.dumps(fetch(), indent=2, default=str))
