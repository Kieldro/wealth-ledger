#!/usr/bin/env python3
"""Budget vs actual: the sheet's monthly budget against posted spending, per category and line.

Budget lines come from the "income sheet" tab of the Google Sheet in personal.json (column A category,
B item, C monthly $; read with `gws`, like networth.py; never written). Actuals are v_ledger class='spend',
pending=0, put on a budget line by regex rules over "payee | description": the owner's own rules from
personal.json "budget.rules" first, then the generic DEFAULT_RULES below. Spending no rule matches is
"Other (unmapped)". Categorization is pattern-based, so it is approximate (e.g. warehouse clubs sell non-food).
Amazon card charges matched to order items (insights/amazon.py) are split across the items' budget lines in
proportion to the item totals instead, so the charge's total is kept.

A rule is [transaction regex, budget-line regex]. The line regex is searched (case-insensitive) in
"category/item" of each sheet line and the first line it matches wins, so "electricity" takes the first
electricity line and "^bills/electricity" picks the one under Bills.

  python -m insights.budget                 this month and last month
  python -m insights.budget --month 2026-09 --json
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import personal  # noqa: E402
from insights import amazon  # noqa: E402

DB = Path(os.environ.get("WEALTH_LEDGER_DB") or Path.home() / ".local/share/wealth-ledger/txns.db")
TAB = "income sheet"
RANGE = f"{TAB}!A1:C200"
UNMAPPED = "Other (unmapped)"
EXCLUDE_CATEGORIES = r"^(saving|savings|investing|investments?)$"  # money set aside, not spent: no actuals
NO_CATEGORY = "Uncategorized"

# Generic merchant patterns (businesses anyone might use). Rules that name the owner's landlord, utilities,
# or people they pay go in personal.json "budget.rules", which are tried first.
DEFAULT_RULES: list[tuple[str, str]] = [
    (r"H-E-B\s*GAS|\bSHELL\b|EXXON|VALERO|BUC-?EE|CHEVRON|\bCIRCLE K\b|\bTEXACO\b|\bSUNOCO\b|SPEEDY STOP"
     r"|COSTCO GAS|QUIKTRIP|\bQT\b|\b7-ELEVEN\b|\bPHILLIPS 66\b|\bCONOCO\b|\bMOBIL\b", r"gasoline|fuel"),
    (r"COSTCO.*MEMBER", r"costco membership"),
    (r"\bH-E-B\b|COSTCO|WHOLE ?FOODS|TRADER JOE|SPROUTS|WALMART|\bALDI\b|CENTRAL MARKET|KROGER|SAFEWAY"
     r"|SUPERMERCA|SUPERMARKET|GROCER|INSTACART", r"groceries"),
    (r"EZ ?TAG|TOLL ?ROAD|TXTAG|\bNTTA\b|E-?ZPASS|SUNPASS|FASTRAK", r"tolls"),
    (r"JPMORGAN ?CHASE.*ACH|MORTGAGE", r"mortgage"),
    (r"HOMESITE|HOMEOWNERS INS", r"home insurance"),
    (r"PROGRESSIVE|STATE FARM|ALLSTATE|\bGEICO\b", r"car insurance"),
    (r"JIFFY LUBE|VALVOLINE|TAKE 5 OIL|OIL CHANGE", r"oil change"),
    (r"CAR ?WASH", r"car wash"),
    (r"DISCOUNT TIRE|\bTIRES?\b", r"tires"),
    (r"PARKMOBILE|PARKING|\bPARK ?WHIZ\b|SPOTHERO", r"(^|/)parking$"),
    (r"\bDMV\b|VEHICLE REG|TXDMV", r"registration"),
    (r"\bUBER\b(?!.*EATS)|\bLYFT\b|LIME\*", r"\bubers?\b|rideshare"),
    (r"AIRLINES?\b|AMERICAN AIR|UNITED AIR|DELTA AIR|SOUTHWEST|SPIRIT AIR|FRONTIER AIR|JETBLUE|ALASKA AIR",
     r"flights?"),
    (r"ANTHROPIC|CLAUDE\.AI", r"claude"),
    (r"OPENROUTER", r"open ?router"),
    (r"YOUTUBE", r"youtube"),
    (r"GOOGLE ?ONE|GOOGLE STORAGE", r"google one"),
    (r"SPOTIFY", r"spotify"),
    (r"AMAZON PRIME|PRIME VIDEO", r"amazon prime"),
    (r"\bBATHE\b", r"bathe"),
    (r"CLOUDFLARE", r"cloudflare"),
    (r"SIMPLEFIN", r"simplefin"),
    (r"\bPLAID\b", r"\bplaid\b"),
    (r"HOSTEX", r"hostex"),
    (r"MONARCH", r"monarch"),
    (r"WELLHUB|GYMPASS", r"wellhub|\bgym\b"),
    (r"PATREON", r"patreon"),
    (r"SUPERWHISPER", r"superwhisper"),
    (r"VENICE\.?AI", r"venice"),
    (r"PRIVATE INTERNET|NORDVPN|MULLVAD|EXPRESSVPN|PROTONVPN", r"\bvpn\b"),
    (r"T-?MOBILE|VERIZON|AT&T WIRELESS|MINT MOBILE|CRICKET", r"cell service|\bphone\b"),
    (r"SPECTRUM|XFINITY|COMCAST|GOOGLE FIBER|AT&T INTERNET|FRONTIER COMM", r"internet"),
    (r"H ?& ?R BLOCK|\bHRB\b|TURBOTAX", r"tax assistance|tax prep"),
    (r"HOME DEPOT|LOWE'?S|APPLIANCE PARTS|ACE HARDWARE", r"house repairs|home improvement"),
    (r"JIU|JITSU|\bBJJ\b|GRACIE", r"jiu jitsu"),
    (r"PINS ?& ?WHEELS|ROLLER|SKATE", r"skating"),
    (r"GRABAGUN|GUNMAG|\bAMMO|MAG SHACK", r"ammunition"),
    (r"BARBER|SUPERCUTS|GREAT CLIPS|HAIRCUT", r"haircut"),
    (r"LAUNDR", r"laundry"),
    (r"GLOBAL ENTRY", r"global entry"),
    (r"PASSPORT|\bPSPT\b", r"passport"),
    (r"RESTAURANT|GRILL|TAQUER|BURGER|PIZZA|\bCAFE\b|COFFEE|STARBUCKS|CHIPOTLE|IN-N-OUT|P\.? ?TERRY|WHATABURGER"
     r"|CHICK-FIL-A|MCDONALD|TACO|\bBBQ\b|SUSHI|RAMEN|DOORDASH|UBER ?EATS|GRUBHUB|SA-T[EÉ]N|LOS VAQUEROS"
     r"|DINER|CANTINA|BAKERY|DONUT", r"restaurant"),
]


# ---------- sheet ----------
def _num(r: list, i: int) -> float | None:
    v = r[i] if len(r) > i else None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_sheet(values: list[list]) -> dict:
    """{"lines": [{category, item, budget}], "sheet_total": the Total row's monthly value or None}.

    Rows are [category, item, monthly]; reading stops at the Total row. Header and spacer rows (no numeric
    monthly amount) are skipped. A blank category becomes NO_CATEGORY. When a header row says "Expense",
    reading starts below it, so an income block above the budget is not read as budget lines; "Expense" in
    the first column means the rows are [item, category, monthly]."""
    lines, total = [], None
    cell = lambda r, i: str(r[i]).strip() if len(r) > i else ""
    start, ci, ii = 0, 0, 1
    for n, r in enumerate(values):
        if "expense" in (cell(r, 0).lower(), cell(r, 1).lower()):
            start = n + 1
            ci, ii = (1, 0) if cell(r, 0).lower() == "expense" else (0, 1)
            break
    for r in values[start:]:
        cat, item = cell(r, ci), cell(r, ii)
        if item.lower() == "total":
            total = _num(r, 2)
            break
        if item and _num(r, 2) is not None:
            lines.append({"category": cat or NO_CATEGORY, "item": item, "budget": _num(r, 2)})
    return {"lines": lines, "sheet_total": total}


def sheet_lines(sheet_id: str | None = None) -> dict:
    """Read the budget tab with gws (read-only, the same access path networth.py uses); see parse_sheet."""
    out = subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "get", "--params",
         json.dumps({"spreadsheetId": sheet_id or personal.sheet_id(), "range": RANGE,
                     "valueRenderOption": "UNFORMATTED_VALUE"})],
        capture_output=True, text=True, timeout=60, check=True).stdout
    return parse_sheet(json.loads(out).get("values", []))


# ---------- rules ----------
def exclude_re() -> re.Pattern:
    """Budget categories that are saving, not spending (personal.json budget.exclude_categories overrides)."""
    return re.compile(personal.section("budget").get("exclude_categories") or EXCLUDE_CATEGORIES, re.I)


def rules() -> list[tuple[str, str]]:
    """personal.json budget.rules (tried first) + DEFAULT_RULES."""
    own = personal.section("budget").get("rules") or []
    for r in own:
        if not (isinstance(r, list) and len(r) == 2):
            raise personal.PersonalConfigError(f"personal.json budget.rules entry needs 2 fields, got {r!r}")
    return [tuple(r) for r in own] + DEFAULT_RULES


def _compile(rule_list):
    return [(re.compile(t, re.I), re.compile(l, re.I)) for t, l in rule_list]


def resolve(lines: list[dict], rule_list) -> list[tuple[re.Pattern, int | None]]:
    """Each rule's transaction regex with the index of the first budget line its line regex matches."""
    out = []
    for txn_re, line_re in _compile(rule_list):
        idx = next((i for i, l in enumerate(lines) if line_re.search(f"{l['category']}/{l['item']}")), None)
        out.append((txn_re, idx))
    return out


def _text(payee: str | None, desc: str | None) -> str:
    return f"{payee or ''} | {desc or ''}"


def default_label(desc: str | None, payee: str | None) -> str:
    s = (payee or "").strip() or (desc or "").strip()
    return re.sub(r"\s*[#*].*$", "", s).strip().title() or "(unknown)"


# ---------- compute ----------
def month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def categorizer(lines: list[dict], rule_list=None, exclude: re.Pattern | None = None):
    """(description, payee) -> the budget line a transaction lands on, or None when no rule matches.

    Same first-match rules and saving-line exclusion as compute(), one transaction at a time (for showing a
    category next to each transaction). An Amazon charge is not split by item here: it gets its merchant's line."""
    exclude = exclude_re() if exclude is None else exclude
    lines = [l for l in lines if not exclude.search(l["category"])]
    resolved = resolve(lines, rules() if rule_list is None else rule_list)

    def line_for(desc: str | None, payee: str | None) -> dict | None:
        text = _text(payee, desc)
        for txn_re, idx in resolved:
            if txn_re.search(text):
                return lines[idx] if idx is not None else None
        return None
    return line_for


def compute(con: sqlite3.Connection, lines: list[dict], month: str, today: date | None = None,
            rule_list=None, since: str | None = None, label=default_label, exclude: re.Pattern | None = None,
            amazon_rules=None) -> dict:
    """Budget vs actual for one month (YYYY-MM).

    lines: parse_sheet()["lines"]. since: first day of complete data (sets partial_data). exclude: category regex
    for saving lines, which are listed but not counted (default exclude_re()). amazon_rules: item rules for split
    Amazon charges (default amazon.item_rules()); a split part is labelled "<merchant>: <item>"."""
    today = today or date.today()
    exclude = exclude_re() if exclude is None else exclude
    excluded = [l for l in lines if exclude.search(l["category"]) and abs(l["budget"]) >= 0.005]
    lines = [l for l in lines if not exclude.search(l["category"])]
    first, last = month_bounds(month)
    days = last.day
    if today < first:
        elapsed = 0
    elif today > last:
        elapsed = days
    else:
        elapsed = today.day
    frac = elapsed / days
    resolved = resolve(lines, rules() if rule_list is None else rule_list)
    targeted = {i for _, i in resolved if i is not None}

    out_lines = [dict(l, actual=0.0, n=0, merchants={}, has_rule=i in targeted) for i, l in enumerate(lines)]
    unmapped = {"actual": 0.0, "n": 0, "merchants": {}}
    splits = amazon.charge_splits(con, lines, first.isoformat(), last.isoformat(), amazon_rules)

    def add(dest, spent, k):
        dest["actual"] += spent
        dest["n"] += 1
        m = dest["merchants"].setdefault(k, [0.0, 0])
        m[0] += spent
        m[1] += 1

    con.row_factory = sqlite3.Row
    for r in con.execute("""SELECT txn_id, date, amt, description, payee FROM v_ledger
                            WHERE class='spend' AND pending=0 AND date BETWEEN ? AND ?""",
                         (first.isoformat(), last.isoformat())):
        spent = -r["amt"]
        k = label(r["description"], r["payee"])
        parts = splits.get(r["txn_id"])
        if parts:  # an Amazon charge matched to its items: each item's share goes to the item's line
            left = spent
            for j, (idx, share, name) in enumerate(parts):
                part = left if j == len(parts) - 1 else round(spent * share, 2)
                left -= part
                add(out_lines[idx] if idx is not None else unmapped, part, f"{k}: {name}")
            continue
        text = _text(r["payee"], r["description"])
        dest = unmapped
        for txn_re, idx in resolved:
            if txn_re.search(text):
                dest = out_lines[idx] if idx is not None else unmapped
                break  # first matching rule decides; a rule whose line isn't in the sheet leaves it unmapped
        add(dest, spent, k)

    def merchants(d, top=8):
        return [{"merchant": k, "spent": round(v[0], 2), "n": v[1]}
                for k, v in sorted(d.items(), key=lambda kv: -kv[1][0])[:top]]

    cats: dict[str, dict] = {}
    for l in out_lines:
        if abs(l["budget"]) < 0.005 and l["n"] == 0:
            continue  # zero-budget line with no spending: noise
        c = cats.setdefault(l["category"], {"category": l["category"], "budget": 0.0, "actual": 0.0, "lines": []})
        c["budget"] += l["budget"]
        c["actual"] += l["actual"]
        c["lines"].append({"item": l["item"], "budget": round(l["budget"], 2), "actual": round(l["actual"], 2),
                           "remaining": round(l["budget"] - l["actual"], 2), "n": l["n"], "has_rule": l["has_rule"],
                           "merchants": merchants(l["merchants"], 5)})
    out_cats = []
    for c in cats.values():
        c["lines"].sort(key=lambda x: (-x["actual"], -x["budget"]))
        out_cats.append(_finish(c, frac))
    out_cats.sort(key=lambda c: -max(c["budget"], c["actual"]))
    if unmapped["n"]:
        out_cats.append(_finish({"category": UNMAPPED, "unmapped": True, "budget": 0.0, "actual": unmapped["actual"],
                                 "n": unmapped["n"], "merchants": merchants(unmapped["merchants"], 15), "lines": []},
                                frac))
    budget_total = sum(l["budget"] for l in lines)
    actual_total = sum(c["actual"] for c in out_cats)
    return {
        "month": month, "days": days, "elapsed_days": elapsed, "current": first <= today <= last,
        "partial_data": bool(since and first.isoformat() < since), "since": since,
        "budget": round(budget_total, 2), "actual": round(actual_total, 2),
        "remaining": round(budget_total - actual_total, 2),
        "expected": round(budget_total * frac, 2),
        "mapped": round(actual_total - unmapped["actual"], 2), "unmapped": round(unmapped["actual"], 2),
        "categories": out_cats,
        "excluded": [{"category": l["category"], "item": l["item"], "budget": round(l["budget"], 2)} for l in excluded],
    }


def _finish(c: dict, frac: float) -> dict:
    c["budget"] = round(c["budget"], 2)
    c["actual"] = round(c["actual"], 2)
    c["remaining"] = round(c["budget"] - c["actual"], 2)
    c["expected"] = round(c["budget"] * frac, 2)
    c["pct"] = round(100 * c["actual"] / c["budget"], 1) if c["budget"] > 0 else None
    c["over"] = c["actual"] > c["budget"] + 0.005
    return c


def months(today: date | None = None) -> tuple[str, str]:
    today = today or date.today()
    return today.strftime("%Y-%m"), (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--month", action="append", help="YYYY-MM (repeatable); default this month and last month")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args(argv)
    sheet = sheet_lines()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    since = personal.section("dashboard").get("ledger_start")
    res = [compute(con, sheet["lines"], m, since=since) for m in (args.month or months())]
    if args.json:
        print(json.dumps(res, indent=1))
        return 0
    for r in res:
        print(f"\n{r['month']}: budget ${r['budget']:,.0f}, spent ${r['actual']:,.0f} "
              f"(${r['unmapped']:,.0f} unmapped), expected by day {r['elapsed_days']}: ${r['expected']:,.0f}"
              f"{'  [partial data]' if r['partial_data'] else ''}")
        for c in r["categories"]:
            print(f"  {c['category'][:28]:28} {c['budget']:>10,.0f} {c['actual']:>10,.0f} {c['remaining']:>10,.0f}"
                  f"{'  OVER' if c['over'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
