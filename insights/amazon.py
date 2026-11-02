#!/usr/bin/env python3
"""Amazon card charges -> the items they paid for, so the budget shows what Amazon spending was for.

Input is Amazon's "Request Your Data -> Your Orders" export (a .zip or its unzipped folder). Only the fields
needed for matching and categorizing are read: order id, order/ship date, ASIN, product name, department,
quantity, item total (incl. tax, shipping and discounts), shipping, discounts, card last 4 and status; for
refunds the order id, refund date, amount and reason. Addresses, gift messages/recipients, tracking numbers,
serial numbers and the like are never read into the database.

Matching: Amazon bills per shipment, so one order can be several card charges. Items are grouped by order + ship
date; a charge matches a shipment group (or the whole order, or failing that a subset of the order's items) when
the amount agrees to the cent and the charge posts -2..+10 days from the ship date (order date if unshipped), on
a card whose last 4 agree with the order's payment method when both are known. Then a looser "points" pass: card
rewards points (usually the previous order's) paid part of many orders, so the card was charged less than the order
total. A still-unmatched charge matches a still-unmatched shipment group (or whole order) when it is below the total
by more than a cent and at most POINTS_BAND (default 15%; personal.json amazon.points_band = [lo, hi] overrides) of
it, same date window and card rule, and only when unambiguous: that group is the charge's only candidate and no other
charge is a candidate for any of its items. Such matches are method "points" with the discount stored; the budget
splits the actual charge pro rata across the items. Positive Amazon credits are matched to refunds like exact
charges. Prime membership, Prime Video, AWS and Kindle/digital charges are classified but not item-matched (the
budget's merchant rules already place them).

Categories: each item gets a budget line of the "income sheet" via [item regex, budget-line regex] rules over
"department | product name": personal.json "amazon.rules" first, then the generic ITEM_RULES below. The first
rule whose item regex matches and whose line regex finds a line in the sheet wins; no match -> "Other (unmapped)".

  python -m insights.amazon import <export.zip | export-dir>    load items/refunds, then match
  python -m insights.amazon match                               re-run matching only
  python -m insights.amazon report [--since 2024-10-01] [--month 2026-10]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import personal  # noqa: E402

DB = Path(os.environ.get("WEALTH_LEDGER_DB") or Path.home() / ".local/share/wealth-ledger/txns.db")
UNMAPPED = "Other (unmapped)"
WINDOW = (-2, 10)          # charge date minus ship (or order) date, in days
SUBSET_MAX_ITEMS = 14      # orders with more open items than this skip the subset-sum search
SUBSET_MAX_STEPS = 20000   # search budget per (charge, order)
TOL = 1                    # cents: Amazon rounds tax per shipment, the export per item
POINTS_BAND = (0.0, 0.15)  # rewards-points discount as a fraction of the group total: charge = total - discount

# ---------- which ledger rows are Amazon ----------
AMAZON_RE = re.compile(r"\bAMAZON|\bAMZN\b|\bAMZN ?MKTP|PRIME ?VIDEO|\bKINDLE\b|\bAWS\b", re.I)
KINDS = [  # first match wins; anything else that looks like Amazon is an item purchase
    ("aws", re.compile(r"\bAWS\b|AMAZON WEB SERVICES", re.I)),
    ("prime_video", re.compile(r"PRIME ?VIDEO", re.I)),
    ("prime", re.compile(r"\bPRIME\b(?!.*MKTP)|PRIME MEMBERSHIP", re.I)),
    ("digital", re.compile(r"\bKINDLE\b|AMZN DIGITAL|AMAZON DIGITAL|DIGITAL SVCS|AUDIBLE", re.I)),
]


def charge_kind(description: str | None, payee: str | None) -> str | None:
    """None (not Amazon) or one of: items, prime, prime_video, aws, digital."""
    text = f"{payee or ''} | {description or ''}"
    if not AMAZON_RE.search(text):
        return None
    return next((k for k, rx in KINDS if rx.search(text)), "items")


# ---------- item categories ----------
# [regex over "department | product name", budget-line regex over "category/item" of the sheet].
# Product keywords first, then departments. Generic only: owner-specific rules go in personal.json amazon.rules.
# a laptop itself, not an accessory "for MacBook": the keyword comes early in the name, before any comma
LAPTOP = (r"\| (?:(?!for\b|compatible\b|with\b)[^|,]){0,30}?\b(macbook|chromebook|laptop)\b"
          r"(?!\s*(stand|sleeve|case|bag|mat|cover|charger|skin|hub|dock|cooling|backpack|riser|lock|screen))")
ITEM_RULES: list[tuple[str, str]] = [
    # product-name keywords (more specific than Amazon's department)
    (r"\bbees?\b|beekeep|bee ?hive|\bhives?\b|apiar|queen (bee|cage)|honey (extractor|frame)", r"bee ?keep|\bbees?\b|apiar"),
    (r"aquari|fish ?tank|aquapon|aquacult|\bkoi\b|fish food", r"aquacult|aquarium|\bfish"),
    (r"bird ?(seed|feeder|food|bath)|\bsuet\b", r"\bbird"),
    (r"\bdogs?\b|\bcats?\b|\bpets?\b|kitty|cat litter|puppy", r"\bpets?\b|\bdogs?\b|\bcats?\b"),
    (r"air filter|furnace filter|hvac filter|\bmerv\b", r"air filter"),
    (r"reverse osmosis|\bro\b.*\b(membrane|filter|system)|water filter|refrigerator filter", r"\bro filter|water filter"),
    (r"mattress(?! (pad|protector|topper|cover))", r"mattress"),
    (LAPTOP, r"laptop"),
    (LAPTOP, r"computer"),
    (r"desktop (pc|computer)|mini pc\b|gaming pc", r"desktop|computer"),
    (r"smart ?watch|apple watch|galaxy watch|fitness tracker", r"smart ?watch|watch"),
    (r"security camera|surveillance|doorbell cam|alarm (system|sensor)|smart lock|deadbolt", r"security"),
    (r"vitamin|supplement|protein powder|creatine|electrolyte|probiotic|collagen|omega-?3|magnesium",
     r"supplement|vitamin|nutrition|^health/"),
    (r"toothpaste|toothbrush|\bfloss|deodorant|shampoo|conditioner|body wash|\bsoap\b|razor|shav(e|ing)|lotion"
     r"|sunscreen|lip balm", r"toiletr|personal care|hygiene"),
    (r"first aid|bandage|thermometer|ibuprofen|acetaminophen|allergy relief|medicine|\bmedical\b|pharmac",
     r"medic|pharmac|^health/"),
    (r"costume|\bparty\b|balloon|decoration|halloween|birthday|banner|confetti|face paint|body paint",
     r"party|hosting|social events?"),
    (r"gift card|gift box|gift wrap|wrapping paper", r"gifts?"),
    (r"pest|ant (killer|bait)|insect|rodent|mouse ?trap|roach", r"pest|house repairs|home repair|home improvement"),
    (r"lawn|garden|\bseeds?\b|potting|\bsoil\b|fertiliz|planter|\bplants?\b|\bhose\b|sprinkler|mulch|\bweed"
     r"|pruning|trimmer", r"/.*(landscap|lawn|yard|garden)"),
    (r"\bcar\b|motor oil|wiper|\btires?\b|jump starter|\bobd|dash ?cam", r"car repair|^car/|\bauto\b|vehicle"),
    (r"\bbooks?\b|audiobook|paperback|hardcover", r"\bbooks?\b|reading|kindle|professional development|education"),
    (r"coffee beans|ground coffee|\bsnacks?\b|candy|chocolate|cereal|\bspices?\b|seasoning|\bjerky\b|beverage"
     r"|drink mix|\bprotein bars?\b|olive oil", r"groceries|/food$"),
    (r"sneaker|\bboots?\b|sandal|slipper|\bshoes?\b", r"shoes?"),
    (r"clothing|shirt|\bpants\b|jacket|\bsocks?\b|underwear|hoodie|sweater|\bhats?\b|\bgloves?\b|sunglasses"
     r"|\bbelts?\b|leggings|\bshorts\b", r"cloth|apparel"),
    (r"\busb\b|hdmi|cable|charger|adapter|\bssd\b|hard drive|memory card|router|keyboard|\bmouse\b|monitor"
     r"|batter(y|ies)|headphone|earbud|speaker|smart plug|zigbee|z-?wave|airtag|smart ?tag|tracker",
     r"electronics?|gadgets?|devices?|\btech\b"),
    (r"drill|screwdriver|\bscrews?\b|wrench|plumbing|electrical|\boutlet|light bulb|caulk|\bpaint\b|adhesive"
     r"|\btape\b|lumber|faucet|hinge|\bglue\b", r"house repairs|home repair|home improvement|maintenance"),
    (r"printer|\bink\b|\bpens?\b|notebook|stapler|label maker|sticky notes", r"office supplies|^office/|/office$"),
    (r"\bchairs?\b|\bdesks?\b|\btables?\b|shelf|shelving|bookcase|sofa|couch|bed frame|mirror", r"furniture"),
    (r"\btoys?\b|lego|puzzle|\bbaby\b|diaper", r"kids|toys?|child|baby"),
    (r"fitness|exercise|yoga|dumbbell|kettlebell|resistance band|weightlifting|camping|hiking",
     r"\bsports?\b|fitness|exercise|outdoor"),
    (r"household|trash bag|paper towel|toilet paper|detergent|cleaner|cleaning|sponge|\bdish|cookware|\bplates?\b"
     r"|utensil|storage|towels?\b|bedding|\bsheets?\b|vacuum|dust bag|\bmop\b|\blamps?\b|\blights?\b|mason jar",
     r"household|home supplies|home goods|kitchen|cleaning supplies|(^|/)misc$"),
    # Amazon's department, when no keyword decided
    (r"^grocery|^gourmet", r"groceries|/food$"),
    (r"^shoes", r"shoes?"),
    (r"^apparel", r"cloth|apparel"),
    (r"^(health|beauty)", r"toiletr|personal care|health|beauty"),
    (r"^(paperback|hardcover|spiral-bound|kindle|book)", r"\bbooks?\b|reading|kindle|professional development"),
    (r"^automotive", r"car repair|^car/|\bauto\b|vehicle"),
    (r"^lawn", r"/.*(landscap|lawn|yard|garden)"),
    (r"^tools", r"house repairs|home repair|home improvement|maintenance"),
    (r"^(electronics|personal computers|computers?)", r"electronics?|gadgets?|devices?|\btech\b"),
    (r"^(electronics|personal computers)", r"computer"),
    (r"^watch", r"smart ?watch|watch"),
    (r"^office product", r"office supplies|^office/|/office$"),
    (r"^(toy|baby product)", r"kids|toys?|child|baby"),
    (r"^sports", r"\bsports?\b|fitness|exercise|outdoor"),
    (r"^jewelry", r"jewel|gifts?"),
    (r"^furniture", r"furniture"),
    (r"^(kitchen|home|misc)", r"household|home supplies|home goods|kitchen|(^|/)misc$"),
]


def item_rules() -> list[tuple[str, str]]:
    """personal.json amazon.rules (tried first) + ITEM_RULES."""
    own = personal.section("amazon").get("rules") or []
    for r in own:
        if not (isinstance(r, list) and len(r) == 2):
            raise personal.PersonalConfigError(f"personal.json amazon.rules entry needs 2 fields, got {r!r}")
    return [tuple(r) for r in own] + ITEM_RULES


def item_text(department: str | None, name: str | None) -> str:
    return f"{department or ''} | {name or ''}"


def resolve_items(lines: list[dict], rule_list=None) -> list[tuple[re.Pattern, int]]:
    """Rules whose line regex finds a sheet line, as (item regex, line index); rules with no line are dropped."""
    out = []
    for item_re, line_re in (item_rules() if rule_list is None else rule_list):
        lr = re.compile(line_re, re.I)
        idx = next((i for i, l in enumerate(lines) if lr.search(f"{l['category']}/{l['item']}")), None)
        if idx is not None:
            out.append((re.compile(item_re, re.I), idx))
    return out


def categorize(department: str | None, name: str | None, resolved) -> int | None:
    """Budget-line index for one item, or None (unmapped)."""
    text = item_text(department, name)
    return next((idx for rx, idx in resolved if rx.search(text)), None)


# ---------- reading the export ----------
NA = {"", "not available", "not applicable", "n/a", "none"}


def _money(v) -> float | None:
    s = str(v or "").strip().strip("'\"").replace(",", "").replace("$", "")
    if s.lower() in NA:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _day(v) -> str | None:
    """ISO timestamp (UTC 'Z' or offset) -> local calendar date; bare dates pass through."""
    s = str(v or "").strip()
    if s.lower() in NA or not re.match(r"\d{4}-\d{2}-\d{2}", s):
        return None
    if len(s) == 10:
        return s
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return s[:10]


def _text(v) -> str | None:
    s = str(v or "").strip()
    return None if s.lower() in NA else s


def _last4(v) -> str | None:
    m = re.findall(r"(\d{4})\b", str(v or ""))
    return m[-1] if m else None


class Export:
    """A Your Orders export as a folder or a .zip; finds the CSVs by name, wherever they sit inside it."""
    FILES = {
        "orders": re.compile(r"(^|/)(Order History|Retail\.OrderHistory\.\d+)\.csv$", re.I),
        "refunds": re.compile(r"(^|/)(Refund Details|Retail\.OrdersReturned\.Payments\.\d+)\.csv$", re.I),
        "digital": re.compile(r"(^|/)(Digital Content Orders|Digital-Ordering\.\d+)\.csv$", re.I),
    }

    def __init__(self, path):
        self.path = Path(path)
        if self.path.is_dir():
            self.names = [str(p.relative_to(self.path)) for p in self.path.rglob("*.csv")]
        elif zipfile.is_zipfile(self.path):
            with zipfile.ZipFile(self.path) as z:
                self.names = [n for n in z.namelist() if n.lower().endswith(".csv")]
        else:
            raise SystemExit(f"error: {path} is neither a folder nor a zip")

    def rows(self, which: str) -> list[dict]:
        rx = self.FILES[which]
        out = []
        for n in sorted(n for n in self.names if rx.search(n.replace("\\", "/"))):
            if self.path.is_dir():
                text = (self.path / n).read_text(encoding="utf-8-sig", errors="replace")
            else:
                with zipfile.ZipFile(self.path) as z:
                    text = z.read(n).decode("utf-8-sig", errors="replace")
            out.extend(csv.DictReader(io.StringIO(text)))
        return out


def _source(website: str | None) -> str:
    w = (website or "").lower()
    return "wholefoods" if "wholefoods" in w else "primenow" if "primenow" in w else "order"


def parse_orders(rows: list[dict]) -> list[dict]:
    """Order History rows -> item dicts (only the fields we keep). line numbers items that share order + ASIN."""
    items = []
    for r in rows:
        oid, asin = _text(r.get("Order ID")), _text(r.get("ASIN")) or "-"
        if not oid:
            continue
        qty = _money(r.get("Original Quantity"))
        unit, tax = _money(r.get("Unit Price")), _money(r.get("Unit Price Tax"))
        ship, disc = _money(r.get("Shipping Charge")) or 0.0, _money(r.get("Total Discounts")) or 0.0
        amount = _money(r.get("Total Amount"))  # per item row: qty x (price + tax) + shipping + discounts
        if amount is None and unit is not None:
            amount = (qty or 1) * (unit + (tax or 0)) + ship - abs(disc)
        items.append({
            "order_id": oid, "asin": asin, "order_date": _day(r.get("Order Date")),
            "ship_date": _day(r.get("Ship Date")), "name": _text(r.get("Product Name")),
            "department": _text(r.get("Department")), "qty": qty, "amount": round(amount or 0.0, 2),
            "shipping": ship, "discounts": disc, "pay_last4": _last4(r.get("Payment Method Type")),
            "status": _text(r.get("Order Status")), "source": _source(r.get("Website")),
        })
    return _number_lines(items, ("order_id", "asin"), ("ship_date", "amount", "name"))


def parse_digital(rows: list[dict]) -> list[dict]:
    """Digital Content Orders: one row per price component (price, tax); summed per digital item."""
    by_item: dict[tuple, dict] = {}
    for r in rows:
        oid = _text(r.get("Order ID"))
        if not oid:
            continue
        key = (oid, _text(r.get("ASIN")) or "-", _text(r.get("Digital Order Item ID")) or "")
        it = by_item.setdefault(key, {
            "order_id": oid, "asin": key[1], "order_date": _day(r.get("Order Date")),
            "ship_date": _day(r.get("Fulfilled Date")), "name": _text(r.get("Product Name")),
            "department": "Digital", "qty": _money(r.get("Original Quantity")) or 1.0, "amount": 0.0,
            "shipping": 0.0, "discounts": 0.0, "pay_last4": _last4(r.get("Payment Information")),
            "status": _text(r.get("Order Status")), "source": "digital", "_k": key[2]})
        it["amount"] = round(it["amount"] + (_money(r.get("Transaction Amount")) or 0.0), 2)
    return _number_lines(list(by_item.values()), ("order_id", "asin"), ("_k",))


def parse_refunds(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        oid = _text(r.get("Order ID"))
        amt = _money(r.get("Refund Amount"))
        if not oid or amt is None:
            continue
        out.append({"order_id": oid, "refund_date": _day(r.get("Refund Date")) or _day(r.get("Creation Date")),
                    "amount": round(amt, 2), "qty": _money(r.get("Quantity")),
                    "reason": _text(r.get("Reversal Reason")), "status": _text(r.get("Payment Status"))})
    return _number_lines(out, ("order_id",), ("refund_date", "amount"))


def _number_lines(rows: list[dict], key: tuple, order: tuple) -> list[dict]:
    """Deterministic `line` per duplicate key, so re-importing the same export upserts the same rows."""
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in key)].append(r)
    out = []
    for g in groups.values():
        g.sort(key=lambda r: tuple(str(r.get(k) or "") for k in order))
        for i, r in enumerate(g):
            r["line"] = i
            r.pop("_k", None)
            out.append(r)
    return out


ITEM_COLS = ("order_id", "asin", "line", "order_date", "ship_date", "name", "department", "qty", "amount",
             "shipping", "discounts", "pay_last4", "status", "source")
REFUND_COLS = ("order_id", "line", "refund_date", "amount", "qty", "reason", "status")


def import_export(con: sqlite3.Connection, path) -> dict:
    ex = Export(path)
    items = parse_orders(ex.rows("orders")) + parse_digital(ex.rows("digital"))
    refunds = parse_refunds(ex.rows("refunds"))
    con.executemany(f"INSERT OR REPLACE INTO amazon_items ({','.join(ITEM_COLS)}) "
                    f"VALUES ({','.join('?' * len(ITEM_COLS))})", [tuple(i[c] for c in ITEM_COLS) for i in items])
    con.executemany(f"INSERT OR REPLACE INTO amazon_refunds ({','.join(REFUND_COLS)}) "
                    f"VALUES ({','.join('?' * len(REFUND_COLS))})", [tuple(r[c] for c in REFUND_COLS) for r in refunds])
    con.commit()
    return {"items": len(items), "refunds": len(refunds)}


# ---------- matching ----------
def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _cents(x: float) -> int:
    return int(round(x * 100))


def _card_last4(account_name: str | None) -> str | None:
    m = re.search(r"\((\d{4})\)\s*$", account_name or "")
    return m.group(1) if m else None


def _in_window(charge: date, ref: date) -> bool:
    return WINDOW[0] <= (charge - ref).days <= WINDOW[1]


def _ref(it: dict) -> date | None:
    s = it["ship_date"] or it["order_date"]
    return _d(s) if s else None


def _subsets(items: list[dict], target: int, ok) -> list[list[dict]]:
    """Subsets of items summing to target cents (amount-desc DFS with bound, capped); ok(subset) filters."""
    items = sorted(items, key=lambda i: -i["c"])
    suffix = [0] * (len(items) + 1)
    for i in range(len(items) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + items[i]["c"]
    found, steps = [], [0]

    def go(i, left, chosen):
        steps[0] += 1
        if steps[0] > SUBSET_MAX_STEPS or len(found) > 3:
            return
        if abs(left) <= TOL and chosen:
            if ok(chosen):
                found.append(list(chosen))
            return
        if i == len(items) or left < -TOL or suffix[i] < left - TOL:
            return
        chosen.append(items[i])
        go(i + 1, left - items[i]["c"], chosen)
        chosen.pop()
        go(i + 1, left, chosen)

    go(0, target, [])
    return found


def points_band() -> tuple[float, float]:
    """(lo, hi) discount fractions for the points pass: personal.json amazon.points_band, else POINTS_BAND."""
    band = personal.section("amazon").get("points_band")
    if band is None:
        return POINTS_BAND
    if not (isinstance(band, list) and len(band) == 2 and 0 <= band[0] <= band[1] < 1):
        raise personal.PersonalConfigError(f"personal.json amazon.points_band must be [lo, hi] with 0 <= lo <= hi < 1, "
                                           f"got {band!r}")
    return float(band[0]), float(band[1])


def _groups(its: list[dict], oday: date, cd: date) -> list[tuple]:
    """(rank, days off, items, method) per shipment group in the date window, plus the whole order when it shipped
    in parts and any of its dates is in the window."""
    groups: dict = defaultdict(list)
    for i in its:
        groups[i["ship_date"]].append(i)
    out = []
    for sd, g in groups.items():
        ref = _d(sd) if sd else oday
        if _in_window(cd, ref):
            out.append((0, abs((cd - ref).days), g, "shipment"))
    refs = {_ref(i) for i in its} | {oday}
    if len(groups) > 1 and any(_in_window(cd, r) for r in refs):
        out.append((1, min(abs((cd - r).days) for r in refs), its, "order"))
    return out


def _charges(con, kinds=("items",)) -> list[dict]:
    con.row_factory = sqlite3.Row
    out = []
    for r in con.execute("SELECT txn_id, date, amt, description, payee, account_name FROM v_ledger "
                         "WHERE pending=0 AND date IS NOT NULL"):
        k = charge_kind(r["description"], r["payee"])
        if k in kinds:
            out.append(dict(r, kind=k, last4=_card_last4(r["account_name"])))
    con.row_factory = None
    return out


def match(con: sqlite3.Connection) -> dict:
    """Rebuild amazon_matches from scratch: charges -> order items, credits -> refunds. Returns counts."""
    charges = _charges(con)
    con.row_factory = sqlite3.Row
    items = [dict(r) for r in con.execute(
        "SELECT * FROM amazon_items WHERE source IN ('order','primenow') AND amount > 0.005 "
        "AND COALESCE(status,'') NOT LIKE 'cancel%' AND order_date IS NOT NULL")]
    refunds = [dict(r) for r in con.execute("SELECT * FROM amazon_refunds WHERE amount > 0.005 AND refund_date IS NOT NULL")]
    con.row_factory = None
    orders: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        it["c"] = _cents(it["amount"])
        it["key"] = (it["order_id"], it["asin"], it["line"])
        orders[it["order_id"]].append(it)
    order_day = {o: min(_d(i["order_date"]) for i in its) for o, its in orders.items()}
    by_day: dict[date, list[str]] = defaultdict(list)
    for o, d in order_day.items():
        by_day[d].append(o)

    def near(ch):
        """(order id, its items, last4 known on both sides) for orders this charge could pay for."""
        cd = _d(ch["date"])
        for k in range(-WINDOW[1] - 45, -WINDOW[0] + 1):
            for o in by_day.get(cd + timedelta(days=k), ()):
                its = orders[o]
                l4 = {i["pay_last4"] for i in its if i["pay_last4"]}
                if ch["last4"] and l4 and ch["last4"] not in l4:
                    continue  # paid with another card
                yield o, its, bool(ch["last4"] and l4)

    cands = []  # (rank, dist, charge txn_id, order_id, item keys, method, last4_known)
    n_cands: dict[str, int] = defaultdict(int)
    for ch in (c for c in charges if c["amt"] < 0):
        cd, target = _d(ch["date"]), _cents(-ch["amt"])
        for o, its, known in near(ch):
            exact = [g for g in _groups(its, order_day[o], cd) if abs(sum(i["c"] for i in g[2]) - target) <= TOL]
            if not exact and len(its) <= SUBSET_MAX_ITEMS and sum(i["c"] for i in its) > target + TOL:
                def ok(sub):
                    rs = [_ref(i) for i in sub]
                    return _in_window(cd, max(rs))
                for sub in _subsets(its, target, ok):
                    ref = max(_ref(i) for i in sub)
                    exact.append((2, abs((cd - ref).days), sub, "subset"))
            for rank, dist, g, method in exact:
                cands.append((rank, dist, ch["txn_id"], o, tuple(sorted(i["key"] for i in g)), method, known))
                n_cands[ch["txn_id"]] += 1

    # greedy: unambiguous charges first, then by method strength and date distance; items are used once
    cands.sort(key=lambda c: (n_cands[c[2]] > 1, c[0], c[1], c[2]))
    used_items, matched = set(), {}
    for rank, dist, tid, o, keys, method, known in cands:
        if tid in matched or used_items.intersection(keys):
            continue
        conf = {"shipment": 0.95, "order": 0.9, "subset": 0.7}[method]
        conf -= 0.2 if n_cands[tid] > 1 else 0
        conf -= 0.1 if not known else 0
        conf -= 0.02 * dist
        matched[tid] = (o, method, round(max(conf, 0.1), 2), json.dumps([[k[1], k[2]] for k in keys]), None)
        used_items.update(keys)

    # points pass: charge below a still-open group's total by a rewards-points discount; unambiguous pairs only
    lo, hi = points_band()
    pairs = []  # (charge txn_id, order_id, item keys, days off, last4_known, discount cents)
    for ch in (c for c in charges if c["amt"] < 0 and c["txn_id"] not in matched):
        cd, target = _d(ch["date"]), _cents(-ch["amt"])
        for o, its, known in near(ch):
            for _rank, dist, g, _method in _groups(its, order_day[o], cd):
                keys = tuple(sorted(i["key"] for i in g))
                total = sum(i["c"] for i in g)
                disc = total - target
                if not used_items.intersection(keys) and disc > TOL and lo * total <= disc <= hi * total:
                    pairs.append((ch["txn_id"], o, keys, dist, known, disc))
    per_charge = defaultdict(int)
    claims: dict = defaultdict(set)  # item key -> charges with a candidate group containing it
    for tid, o, keys, *_ in pairs:
        per_charge[tid] += 1
        for k in keys:
            claims[k].add(tid)
    for tid, o, keys, dist, known, disc in pairs:
        if per_charge[tid] == 1 and all(claims[k] == {tid} for k in keys):
            conf = 0.6 - (0.1 if not known else 0) - 0.02 * dist
            matched[tid] = (o, "points", round(max(conf, 0.1), 2), json.dumps([[k[1], k[2]] for k in keys]),
                            round(disc / 100, 2))
            used_items.update(keys)

    # refunds: a credit = one refund, or the refunds of one order on one day
    rgroups: dict = defaultdict(list)
    for r in refunds:
        rgroups[(r["order_id"], r["refund_date"])].append(r)
    rcands = []
    for ch in (c for c in charges if c["amt"] > 0):
        cd, target = _d(ch["date"]), _cents(ch["amt"])
        for (o, rd), rs in rgroups.items():
            if not _in_window(cd, _d(rd)):
                continue
            opts = [[r] for r in rs] + ([rs] if len(rs) > 1 else [])
            for opt in opts:
                if abs(sum(_cents(r["amount"]) for r in opt) - target) <= TOL:
                    rcands.append((abs((cd - _d(rd)).days), ch["txn_id"], o, tuple(r["line"] for r in opt)))
    rn = defaultdict(int)
    for c in rcands:
        rn[c[1]] += 1
    used_ref = set()
    for dist, tid, o, lines in sorted(rcands, key=lambda c: (rn[c[1]] > 1, c[0])):
        if tid in matched or any((o, ln) in used_ref for ln in lines):
            continue
        matched[tid] = (o, "refund", round(0.9 - (0.2 if rn[tid] > 1 else 0) - 0.02 * dist, 2), json.dumps(lines),
                        None)
        used_ref.update((o, ln) for ln in lines)

    con.execute("DELETE FROM amazon_matches")
    con.executemany("INSERT INTO amazon_matches (txn_id, order_id, method, confidence, items, discount) "
                    "VALUES (?,?,?,?,?,?)",
                    [(t, *v) for t, v in matched.items()])
    con.commit()
    methods = defaultdict(int)
    for v in matched.values():
        methods[v[1]] += 1
    return {"charges": sum(1 for c in charges if c["amt"] < 0), "credits": sum(1 for c in charges if c["amt"] > 0),
            "matched": dict(methods),
            "points_discount_usd": round(sum(v[4] or 0 for v in matched.values()), 2)}


# ---------- splitting a matched charge across its items (used by insights/budget.py) ----------
def charge_splits(con: sqlite3.Connection, lines: list[dict], first: str, last: str,
                  rule_list=None) -> dict[str, list[tuple[int | None, float, str]]]:
    """{txn_id: [(budget-line index or None, share, item name), ...]} for matched charges dated first..last.

    Shares are proportional to the item totals and sum to 1, so splitting keeps the charge's total. Returns {}
    when the Amazon tables don't exist yet (read-only connection to a database that predates them)."""
    try:
        rows = con.execute("SELECT m.txn_id, m.order_id, m.items FROM amazon_matches m JOIN v_ledger l "
                           "ON l.txn_id = m.txn_id WHERE m.method != 'refund' AND l.date BETWEEN ? AND ?",
                           (first, last)).fetchall()
    except sqlite3.OperationalError:
        return {}
    if not rows:
        return {}
    resolved = resolve_items(lines, rule_list)
    out = {}
    for tid, oid, keys_json in rows:
        keys = {tuple(k) for k in json.loads(keys_json or "[]")}
        its = [r for r in con.execute("SELECT asin, line, name, department, amount FROM amazon_items "
                                      "WHERE order_id = ?", (oid,)).fetchall()
               if not keys or (r[0], r[1]) in keys]
        its = [r for r in its if (r[4] or 0) > 0] or its
        if not its:
            continue
        total = sum(r[4] or 0 for r in its)
        parts = [(categorize(r[3], r[2], resolved), (r[4] or 0) / total if total > 0 else 1 / len(its),
                  short_name(r[2])) for r in its]
        out[tid] = parts
    return out


def short_name(name: str | None, n: int = 48) -> str:
    s = re.split(r"\s*[,|(\[]\s*", (name or "").strip())[0] or (name or "item")
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---------- report ----------
def report(con: sqlite3.Connection, since: str, month: str | None, lines: list[dict] | None) -> dict:
    charges = [c for c in _charges(con, kinds=("items", "prime", "prime_video", "aws", "digital"))
               if c["date"] >= since]
    m = {r[0]: (r[1], r[2] or 0.0) for r in con.execute("SELECT txn_id, method, discount FROM amazon_matches")}
    spend = [c for c in charges if c["amt"] < 0 and c["kind"] == "items"]
    hit = [c for c in spend if c["txn_id"] in m]
    kinds = defaultdict(lambda: [0, 0.0])
    for c in charges:
        if c["amt"] < 0:
            kinds[c["kind"]][0] += 1
            kinds[c["kind"]][1] += -c["amt"]
    credits = [c for c in charges if c["amt"] > 0 and c["kind"] == "items"]
    res = {"since": since, "item_charges": len(spend), "matched": len(hit),
           "match_rate": round(100 * len(hit) / len(spend), 1) if spend else None,
           "matched_usd": round(-sum(c["amt"] for c in hit), 2), "charged_usd": round(-sum(c["amt"] for c in spend), 2),
           "unmatched": len(spend) - len(hit),
           "unmatched_usd": round(-sum(c["amt"] for c in spend if c["txn_id"] not in m), 2),
           "points_matched": sum(1 for c in hit if m[c["txn_id"]][0] == "points"),
           "points_discount_usd": round(sum(m[c["txn_id"]][1] for c in hit), 2), "credits": len(credits),
           "credits_matched": sum(1 for c in credits if c["txn_id"] in m),
           "kinds": {k: {"n": v[0], "usd": round(v[1], 2)} for k, v in kinds.items()}}
    if month and lines is not None:
        from insights import budget  # local import: budget imports this module
        first, last = budget.month_bounds(month)
        splits = charge_splits(con, lines, first.isoformat(), last.isoformat())
        cat, unm = defaultdict(float), defaultdict(float)
        mspend = [c for c in spend if first.isoformat() <= c["date"] <= last.isoformat()]
        for c in mspend:
            for idx, share, name in splits.get(c["txn_id"], [(None, 1.0, "(charge not matched)")]):
                key = f"{lines[idx]['category']}/{lines[idx]['item']}" if idx is not None else UNMAPPED
                cat[key] += -c["amt"] * share
                if idx is None:
                    unm[name] += -c["amt"] * share
        res["month"] = {"month": month, "total": round(-sum(c["amt"] for c in mspend), 2),
                        "by_line": {k: round(v, 2) for k, v in sorted(cat.items(), key=lambda kv: -kv[1])},
                        "unmapped_items": {k: round(v, 2) for k, v in sorted(unm.items(), key=lambda kv: -kv[1])}}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(DB))
    sub = ap.add_subparsers(dest="cmd", required=True)
    ip = sub.add_parser("import", help="load an Amazon Your Orders export (.zip or folder), then match")
    ip.add_argument("path")
    sub.add_parser("match", help="re-run charge/refund matching")
    rp = sub.add_parser("report", help="match rate and a month's Amazon spend by budget line")
    rp.add_argument("--since", default="2024-10-01")
    rp.add_argument("--month")
    rp.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    import ctdb
    con = ctdb.connect(args.db)
    if args.cmd == "import":
        print("imported", import_export(con, args.path))
        print("matched", match(con))
    elif args.cmd == "match":
        print("matched", match(con))
    else:
        lines = None
        if args.month:
            from insights import budget
            lines = [l for l in budget.sheet_lines()["lines"] if not budget.exclude_re().search(l["category"])]
        r = report(con, args.since, args.month, lines)
        if args.json:
            print(json.dumps(r, indent=1))
        else:
            print(f"Amazon item charges since {r['since']}: {r['matched']}/{r['item_charges']} matched "
                  f"({r['match_rate']}%), ${r['matched_usd']:,.2f} of ${r['charged_usd']:,.2f}; "
                  f"{r['unmatched']} unmatched (${r['unmatched_usd']:,.2f}); credits {r['credits_matched']}/"
                  f"{r['credits']} matched to refunds")
            print(f"  {r['points_matched']} matched below the order total, paid partly with rewards points "
                  f"(${r['points_discount_usd']:,.2f} in points)")
            for k, v in r["kinds"].items():
                print(f"  {k:12} {v['n']:4} charges  ${v['usd']:,.2f}")
            if "month" in r:
                mo = r["month"]
                print(f"\n{mo['month']} Amazon item charges ${mo['total']:,.2f} by budget line:")
                for k, v in mo["by_line"].items():
                    print(f"  {k[:50]:50} ${v:>9,.2f}")
                print(f"\nunmapped items ({len(mo['unmapped_items'])}):")
                for k, v in list(mo["unmapped_items"].items())[:40]:
                    print(f"  ${v:>8,.2f}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
