"""Car values from live comparable listings (AutoTempest aggregate), mileage-adjusted, minus a dealer discount.

Source (free, no account, verified 2026-10-21): autotempest.com/results aggregates CarGurus, Cars.com,
TrueCar, Carvana, CarMax, eBay Motors, etc. Each listing carries year, trim, mileage, price, seller type,
title status, VIN and the source URL. The page fetches results with per-request tokens, so one headless
Chromium visit per car reads the page's own `queue-results` JSON. Cars.com, CarGurus, TrueCar, Carvana,
Carfax, Edmunds and KBB block direct requests (403 / DataDome); Craigslist's API works but is too thin
and spammy; SerpApi's eBay engine returns parts, not cars. KBB/Edmunds have no free API.

Method, per comp pool (all knobs are per car in personal.json and echoed into `detail`):
  1. comps = in-state listings (STATE), same model, matching trim, clean title, years in range, deduped by VIN;
     optionally filtered on the comps' VIN-decoded cab / drive / engine (NHTSA vPIC, free, no key).
  2. year-normalise to the car's model year (year_adj per year of difference).
  3. mileage slope = Theil-Sen (median pairwise) $/mile over the pool, clamped to SLOPE_RANGE.
  4. each comp's price is moved to the car's estimated mileage (comps within 50k mi, else nearest 10).
  5. outliers > 3 MAD from the median are dropped; retail = median.
  6. private-party value = retail x (1 - DEALER_DISCOUNT) for dealer asks (asking prices run above
     private-party / KBB values).
A car's value is the mean of its pools. A rare configuration can use two pools that each differ from it in one
way (e.g. a bigger cab, a smaller engine); the midpoint is the conservative middle.
Current mileage = the latest known odometer reading + a typical miles/year rate.

The cars themselves (balance-sheet label, VIN, trim, odometer, pools) and the search ZIP/radius/state come
from personal.json "cars" (see personal.example.json). Each car entry:
  asset, vehicle, vin, vin_source, make, model (AutoTempest slugs), year, years [min, max], year_adj (optional),
  odometer [miles, "YYYY-MM-DD", source], miles_per_year, sanity (optional),
  pools: [{name, trim_ok (regex), reject (regex), vin (optional {cab, drive, min_l, max_l})}]
"""
from __future__ import annotations

import os
import re
import statistics
import sys
from datetime import date
from itertools import combinations
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import personal  # noqa: E402

_CFG = personal.section("cars")
NAME = "cars"
EVERY_DAYS = 30
CARS: list[dict] = personal.require("cars", "cars")
ASSETS = tuple(c["asset"] for c in CARS)

ZIP, RADIUS = str(personal.require("cars", "zip")), int(_CFG.get("radius", 150))
STATE = personal.require("cars", "state")  # two-letter code; comps must be listed in this state
DEALER_DISCOUNT = 0.12        # dealer asking -> private-party value
SLOPE_RANGE = (-0.25, -0.02)  # $/mile
MIN_COMPS = 5
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"


def est_miles(car) -> tuple[int, str]:
    miles, d, _ = car["odometer"]
    days = (date.today() - date.fromisoformat(d)).days
    est = int(round(miles + car["miles_per_year"] * days / 365.25, -2))
    return est, f"{miles:,} mi on {d} + {car['miles_per_year']:,} mi/yr x {days} days (estimate)"


def search_url(car) -> str:
    q = {"make": car["make"], "model": car["model"], "minyear": car["years"][0], "maxyear": car["years"][1],
         "zip": ZIP, "radius": RADIUS}
    return "https://www.autotempest.com/results?" + urlencode(q)


def listings(urls: list[str]) -> dict[str, list[dict]]:
    from playwright.sync_api import sync_playwright

    out: dict[str, list[dict]] = {u: [] for u in urls}
    env = {k: v for k, v in os.environ.items() if k != "DISPLAY"}  # headless only, never the desktop
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, env=env)
        try:
            ctx = b.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
            for u in urls:
                pg = ctx.new_page()
                bucket = out[u]

                def on_resp(r, bucket=bucket):
                    if "queue-results" in r.url:
                        try:
                            bucket.extend(r.json().get("results") or [])
                        except Exception:
                            pass
                pg.on("response", on_resp)
                pg.goto(u, wait_until="domcontentloaded", timeout=60000)
                last, stable = -1, 0
                for _ in range(40):  # wait until the result batches stop arriving
                    pg.wait_for_timeout(1000)
                    stable = stable + 1 if len(bucket) == last else 0
                    last = len(bucket)
                    if last and stable >= 5:
                        break
                pg.close()
        finally:
            b.close()
    return out


def num(s) -> float | None:
    s = re.sub(r"[^\d.]", "", str(s or ""))
    return float(s) if s else None


def vin_decode(vins: list[str]) -> dict[str, dict]:
    """NHTSA vPIC batch decode (free, no key): cab style, drive and engine size per VIN."""
    import requests

    out = {}
    for i in range(0, len(vins), 50):
        r = requests.post("https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/",
                          data={"format": "json", "DATA": ";".join(vins[i:i + 50])}, timeout=60)
        r.raise_for_status()
        for x in r.json()["Results"]:
            out[x["VIN"]] = {"cab": x.get("BodyCabType") or "", "drive": x.get("DriveType") or "",
                             "liters": num(x.get("DisplacementL"))}
    return out


def vin_ok(dec: dict | None, want: dict) -> bool:
    if not dec:
        return False
    if "cab" in want and want["cab"] not in dec["cab"].lower():
        return False
    if "drive" in want and want["drive"] not in dec["drive"].lower():
        return False
    liters = dec["liters"] or 0
    if "min_l" in want and liters < want["min_l"]:
        return False
    if "max_l" in want and not (0 < liters <= want["max_l"]):
        return False
    return True


def candidates(car, raw: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in raw:
        key = r.get("vin") or r.get("id")
        price, miles, yr = num(r.get("price")), num(r.get("mileage")), int(num(r.get("year")) or 0)
        if key in seen or not price or not miles or price < 2000:
            continue
        if not (car["years"][0] <= yr <= car["years"][1]) or not str(r.get("location", "")).endswith(f", {STATE}"):
            continue
        if (r.get("vehicleTitle") or "Clean") not in ("Clean", "Unknown"):
            continue
        seen.add(key)
        yf = (1 + car.get("year_adj", 0)) ** (car["year"] - yr)
        out.append({"title": r.get("title") or "", "trim": (r.get("trim") or "").strip(), "year": yr,
                    "miles": int(miles), "price": price, "price_year_adj": round(price * yf),
                    "seller": r.get("sellerType"), "dealer": r.get("dealerName"), "location": r.get("location"),
                    "source": r.get("sourceName"), "vin": r.get("vin"), "url": r.get("url")})
    return out


def value_pool(pool: dict, comps: list[dict], target: int) -> dict:
    comps = [c for c in comps if re.search(pool["trim_ok"], c["trim"], re.I)
             and not re.search(pool["reject"], f"{c['trim']} {c['title']}", re.I)
             and ("vin" not in pool or vin_ok(c.get("decoded"), pool["vin"]))]
    if len(comps) < MIN_COMPS:
        raise RuntimeError(f"pool '{pool['name']}': only {len(comps)} comps")
    pairs = [(b["price_year_adj"] - a["price_year_adj"]) / (b["miles"] - a["miles"])
             for a, b in combinations(comps, 2) if abs(b["miles"] - a["miles"]) >= 5000]
    raw_slope = statistics.median(pairs) if pairs else SLOPE_RANGE[1]
    slope = min(max(raw_slope, SLOPE_RANGE[0]), SLOPE_RANGE[1])
    near = [c for c in comps if abs(c["miles"] - target) <= 50000]
    if len(near) < 6:
        near = sorted(comps, key=lambda c: abs(c["miles"] - target))[:10]
    used = []
    for c in near:
        adj = c["price_year_adj"] + slope * (target - c["miles"])
        disc = DEALER_DISCOUNT if c["seller"] != "Private" else 0
        used.append({**c, "adjusted_retail": round(adj), "private_party": round(adj * (1 - disc))})
    med = statistics.median(c["private_party"] for c in used)
    mad = statistics.median(abs(c["private_party"] - med) for c in used) or 1
    kept = [c for c in used if abs(c["private_party"] - med) <= 3 * mad]
    gap = statistics.median(abs(c["miles"] - target) for c in kept)
    return {"name": pool["name"], "value": statistics.median(c["private_party"] for c in kept),
            "median_mileage_gap": round(gap),
            **({"extrapolated": f"comps are a median {gap:,.0f} mi from the target; mileage slope does the work"}
               if gap > 40000 else {}),
            "retail_median": round(statistics.median(c["adjusted_retail"] for c in kept)),
            "slope_per_mile": round(slope, 4), "slope_raw": round(raw_slope, 4),
            "n_pool": len(comps), "n_used": len(kept),
            "outliers_dropped": [c["vin"] for c in used if c not in kept],
            "comps": sorted(kept, key=lambda c: c["miles"])}


def value_car(car, raw: list[dict]) -> dict:
    target, how = est_miles(car)
    comps = candidates(car, raw)
    if any("vin" in p for p in car["pools"]):
        dec = vin_decode([c["vin"] for c in comps if c["vin"]])
        for c in comps:
            c["decoded"] = dec.get(c["vin"])
    pools, errors = [], []
    for p in car["pools"]:
        try:
            pools.append(value_pool(p, comps, target))
        except RuntimeError as e:
            errors.append(str(e))
    if not pools:
        raise RuntimeError(f"{car['asset']}: " + "; ".join(errors))
    value = round(statistics.mean(p["value"] for p in pools), -2)
    detail = {
        "vehicle": car["vehicle"], "vin": car["vin"], "vin_source": car["vin_source"],
        "est_miles": target, "miles_how": how, "odometer_source": car["odometer"][2],
        "method": (f"{STATE} comps, year-normalised, moved to est. mileage with a Theil-Sen $/mi slope, 3-MAD "
                   f"outliers dropped, median, minus {DEALER_DISCOUNT:.0%} dealer-ask discount; mean of pools"),
        "pools": {p["name"]: round(p["value"]) for p in pools},
        "pool_detail": pools, "n_candidates": len(comps),
        "search_url": search_url(car), "sanity_check": car.get("sanity"),
    }
    if errors:
        detail["pool_errors"] = errors
    if len(pools) > 1:
        lo, hi = min(p["value"] for p in pools), max(p["value"] for p in pools)
        detail["bracket"] = f"${lo:,.0f}-${hi:,.0f}; value is the midpoint (conservative middle)"
    return {"asset": car["asset"], "value": value, "source": "autotempest-comps", "detail": detail}


def fetch() -> list[dict]:
    urls = [search_url(c) for c in CARS]
    got = listings(urls)
    rows, errors = [], []
    for car, u in zip(CARS, urls):
        try:
            rows.append(value_car(car, got[u]))
        except Exception as e:
            errors.append(str(e))
    if not rows:
        raise RuntimeError("; ".join(errors) or "no car values")
    for e in errors:
        print(f"warning: cars: {e}")
    return rows


if __name__ == "__main__":
    for r in fetch():
        d = r["detail"]
        print(f"{r['asset']}: ${r['value']:,.0f}  est {d['est_miles']:,} mi  pools {d['pools']}  {d.get('bracket', '')}")
        for p in d["pool_detail"]:
            print(f"  [{p['name']}] slope {p['slope_per_mile']}/mi  retail ${p['retail_median']:,}  "
                  f"{p['n_used']}/{p['n_pool']} comps, dropped {p['outliers_dropped']}")
            for c in p["comps"]:
                print(f"     {c['year']} {c['trim'][:12]:12} {c['miles']:>7,} mi  ${c['price']:>7,.0f}"
                      f"  -> ${c['private_party']:>7,}  {c['location']}")
