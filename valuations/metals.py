"""Precious metals at live spot: troy oz from the sheet's `metals` tab x free keyless spot quotes.

Holdings come from the itemized purchase log on the `metals` tab. Each item row has
Element (B: Ag/Au/Pt/Pd/Cu), Qty held (G, 0 = no longer held) and Troy Oz of fine metal per item (H,
already the melt content of one item). The sheet's own stack totals (col M)
are SUM(G*H) per element and the balance sheet's "Total metals value" is spot x those oz, with no
premiums, so value = sum(oz * spot) here too. The log ends at the "Silver Spot currently" row; rows
below it (spot cells, calculators, notes) are not holdings.

Spot: Swissquote's public bid/ask feed (mid), falling back per metal to gold-api.com, then Yahoo
front-month futures. All keyless.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import personal  # noqa: E402

NAME = "metals"
EVERY_DAYS = 1
ASSETS = ("Total metals value",)

METALS = {"Ag": "silver", "Au": "gold", "Pt": "platinum", "Pd": "palladium", "Cu": "copper"}
SWISSQUOTE = {"Au": "XAU", "Ag": "XAG", "Pt": "XPT", "Pd": "XPD"}
GOLD_API = {"Au": "XAU", "Ag": "XAG", "Pt": "XPT", "Pd": "XPD", "Cu": "HG"}  # HG is USD/lb, not used for oz
YAHOO = {"Au": "GC=F", "Ag": "SI=F", "Pt": "PL=F", "Pd": "PA=F"}
# metals tab rows naming the sheet's spot cells (column C) and its per-element stack totals (column M)
SPOT_LABELS = {"Silver Spot currently": "Ag", "Gold Spot currently": "Au",
               "Platinum Spot currently": "Pt", "Palladium spot currently": "Pd"}
TOTAL_LABELS = {"ag stack total": "Ag", "au stack total": "Au", "pt stack total": "Pt", "pd stack total": "Pd"}
UA = {"User-Agent": "Mozilla/5.0 (wealth-ledger valuations)"}


def _sheet(rng: str) -> list[list]:
    out = subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "get", "--params",
         json.dumps({"spreadsheetId": personal.sheet_id(), "range": rng, "valueRenderOption": "UNFORMATTED_VALUE"})],
        capture_output=True, text=True, check=True, timeout=60).stdout
    return json.loads(out).get("values", [])


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def holdings() -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """-> (oz per element from item rows, the sheet's stack-total oz, the sheet's spot prices)."""
    oz: dict[str, float] = {}
    stack: dict[str, float] = {}
    spot: dict[str, float] = {}
    in_log = True
    for r in _sheet("metals!A1:Z400")[1:]:
        r = list(r) + [""] * (13 - len(r))
        label = str(r[0]).strip()
        if label in SPOT_LABELS:
            in_log = False  # everything from here down is spot cells, calculators, notes
            if _num(r[2]) is not None:
                spot[SPOT_LABELS[label]] = _num(r[2])
            continue
        if label.lower() in TOTAL_LABELS:
            stack[TOTAL_LABELS[label.lower()]] = _num(r[12]) or 0.0
            continue
        el = str(r[1]).strip()
        if not in_log or el not in METALS:
            continue
        qty, per = _num(r[6]), _num(r[7])
        if qty is None or per is None:
            continue
        oz[el] = oz.get(el, 0.0) + qty * per
    return oz, stack, spot


def _get(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as f:
        return json.load(f)


def _swissquote(el: str) -> float:
    quotes = _get(f"https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/{SWISSQUOTE[el]}/USD")
    p = quotes[0]["spreadProfilePrices"][0]
    return (float(p["bid"]) + float(p["ask"])) / 2


def _gold_api(el: str) -> float:
    return float(_get(f"https://api.gold-api.com/price/{GOLD_API[el]}")["price"])


def _yahoo(el: str) -> float:
    d = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{YAHOO[el]}?interval=1d&range=1d")
    return float(d["chart"]["result"][0]["meta"]["regularMarketPrice"])


SOURCES = [("swissquote", _swissquote, SWISSQUOTE), ("gold-api.com", _gold_api, GOLD_API),
           ("yahoo futures", _yahoo, YAHOO)]
# sanity band per troy oz; a quote outside it is treated as a failed source
PLAUSIBLE = {"Au": (1000, 20000), "Ag": (10, 500), "Pt": (300, 10000), "Pd": (300, 10000)}


def spot_price(el: str) -> tuple[float, str]:
    errors = []
    for name, fn, table in SOURCES:
        if el not in table or el not in PLAUSIBLE:
            continue
        try:
            px = fn(el)
        except Exception as e:  # noqa: BLE001 - try the next source
            errors.append(f"{name}: {e}")
            continue
        lo, hi = PLAUSIBLE[el]
        if lo <= px <= hi:
            return px, name
        errors.append(f"{name}: implausible {px}")
    raise RuntimeError(f"no spot price for {METALS[el]}: " + "; ".join(errors))


def fetch() -> list[dict]:
    oz, stack, sheet_spot = holdings()
    oz = {el: v for el, v in oz.items() if abs(v) > 1e-9}
    if not oz:
        raise RuntimeError("no metal holdings parsed from the metals tab")
    for el, v in stack.items():  # the parse must agree with the sheet's own stack totals
        if abs(oz.get(el, 0.0) - v) > 1e-6:
            raise RuntimeError(f"{METALS[el]}: parsed {oz.get(el, 0.0):.6f} oz but sheet stack total says {v:.6f}")

    bs = {str(r[0]).strip().rstrip("*"): r[1] for r in _sheet("balance sheet!A1:B140") if len(r) >= 2}
    sheet_total = _num(bs.get("Total metals value"))

    metals, total, at_sheet_spot = {}, 0.0, 0.0
    for el in sorted(oz, key=lambda e: list(METALS).index(e)):
        px, src = spot_price(el)
        val = oz[el] * px
        total += val
        if el in sheet_spot:
            at_sheet_spot += oz[el] * sheet_spot[el]
        metals[METALS[el]] = {"oz": round(oz[el], 4), "spot": round(px, 2), "source": src,
                              "value": round(val, 2), "sheet_spot": sheet_spot.get(el)}
    srcs = sorted({m["source"] for m in metals.values()})
    return [{
        "asset": "Total metals value",
        "value": round(total, 2),
        "source": "spot: " + ", ".join(srcs),
        "detail": {
            "metals": metals,
            "sheet_total": sheet_total,
            "total_at_sheet_spot": round(at_sheet_spot, 2),
            "premiums": "none (melt value, same as the sheet)",
        },
    }]


if __name__ == "__main__":
    print(json.dumps(fetch(), indent=1))
