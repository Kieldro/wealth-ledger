#!/usr/bin/env python3
"""Subscriptions and unusual charges, from card and checking spending (v_ledger class='spend').

Subscriptions: charges at one merchant that repeat monthly (26-36 days apart) or yearly (350-380 days) at a similar
amount, found with insights.overdraft's series_key/chains. A monthly series counts when it is still active and
either (a) mostly the same price (at least half the steps within 3% or $1) or (b) 3+ charges that stay within 1.6x
of each other (metered bills); a yearly one when its renewals stay within 1.2x. A merchant where the series is
buried in other similar-sized purchases (groceries, fuel, prepaid top-ups: more of them than half the series, from
one interval before it to one after) is skipped, and yearly series need a merchant used at most ~twice a month.
Two charges are enough only when they are the same price and the merchant has nothing else similar around them.
Each one is tagged:
  subscription  the default
  bill          utilities, insurance, phone/internet, housing (by keyword), or any checking-side debit >= $500
  person        Zelle / Venmo / Cash App / PayPal transfers to a person
Bills and transfers stay in the list (they're recurring money out too) but have their own totals, so the
"subscriptions" total is only the things you could cancel. personal.json can add owner-specific bill keywords:
{"subscriptions": {"bill_patterns": ["regex", ...]}}.
Flags: price increase vs the previous charge (fixed-price series only); new = first charge in the last 60 days with
no earlier similar charge at that merchant.

Unusual charges in the last 30 days (short list, most notable first):
  fee        interest, late, returned-payment/NSF/overdraft, foreign-transaction and other bank fees
  duplicate  same merchant and amount within 3 days ($15+, and not an amount that merchant charges routinely)
  large      over $50, over 2x that merchant's median and above its 95th percentile (3+ earlier charges)
Transfers to people (Zelle, Venmo...) are left out of duplicate/large: their "merchant" is many recipients.

  python -m insights.subscriptions          text report
  python -m insights.subscriptions --json
Read-only. Never sends notifications (owner's rule).
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta

import personal
from insights.overdraft import DB, chains, label, series_key

LOOKBACK = 800              # days of history: two years so yearly renewals show up
KINDS = ("credit", "checking")
CADENCES = {"monthly": ((26, 36), 30.4, 14), "yearly": ((350, 380), 365.0, 30)}  # gaps, expected gap, late grace
MAX_RATIO = 1.5             # consecutive charges in one series (lets a price increase stay in its series)
FLAT_PCT, FLAT_ABS = 0.03, 1.0
VARIES_RATIO = 1.6
YEARLY_BUSY = 24            # yearly series only at merchants with at most ~2 charges a month
YEARLY_RATIO = 1.2          # renewal price hikes are usually smaller; bigger jumps a year apart are shop purchases
NEW_DAYS = 60
WINDOW = 30                 # unusual charges: days back
DUP_DAYS, DUP_MIN = 3, 15.0
ROUTINE = 3                 # an exact amount seen this many times before is routine (toll top-ups, parking)
BIG_FACTOR, BIG_MIN, BIG_HISTORY = 2.0, 50.0, 3
MAX_UNUSUAL = 12
BILL_CHECKING_MIN = 500.0

GENERIC_PAYEES = {"investment buy", "investment sell", "payment", "transfer", "deposit", "withdrawal"}
PERSON_RE = re.compile(r"\b(ZELLE|VENMO|CASH ?APP|PAYPAL)\b", re.I)
BILL_RE = re.compile(r"ELECTRIC|UTILIT|\bWATER\b|ENERGY|\bPOWER\b|NATURAL GAS|\bGAS CO|INSURANC|MORTGAGE|\bRENT\b|"
                     r"\bLEASE\b|\bHOA\b|PROPERTY MGMT|INTERNET|WIRELESS|MOBILE|TELECOM|\bCABLE\b|BROADBAND|"
                     r"WASTE|TRASH|SEWER|\bLOAN\b", re.I)
FEE_TYPES = [  # (type, label, pattern); first match wins
    ("overdraft", "Overdraft / NSF fee", r"OVERDRAFT|\bOD FEE|\bNSF\b|INSUFFICIENT FUNDS"),
    ("returned", "Returned payment fee", r"RETURN(ED)? (PAYMENT|ITEM|CHECK|ACH)\b.*\b(FEE|CHARGE)"),
    ("late", "Late fee", r"\bLATE (PAYMENT )?(FEE|CHARGE)"),
    ("foreign", "Foreign transaction fee", r"FOREIGN (TRANSACTION|TRANS|TXN|EXCHANGE|CURRENCY)|\bFX FEE|"
                                           r"INTERNATIONAL (TRANSACTION|SERVICE) FEE|INTL (TRANSACTION|TXN|SVC|SERVICE)? ?FEE|"
                                           r"CURRENCY CONVERSION"),
    ("interest", "Interest charge", r"INTEREST CHARGE|FINANCE CHARGE|\bINTEREST\b"),
    ("fee", "Bank fee", r"\b(ANNUAL|MEMBERSHIP|SERVICE|MAINTENANCE|MONTHLY|ATM|WIRE|CASH ADVANCE|BALANCE TRANSFER|"
                        r"PAPER STATEMENT|STOP PAYMENT|ACCOUNT) (FEE|CHARGE)S?\b|\bFEE CHARGED\b"),
]
FEE_TYPES = [(t, lbl, re.compile(p, re.I)) for t, lbl, p in FEE_TYPES]


def merchant_key(desc: str, payee: str | None) -> str:
    """Grouping key: the feed's payee when it has a real one (it merges a merchant's description variants)."""
    p = (payee or "").strip()
    return series_key(desc if not p or p.lower() in GENERIC_PAYEES else p)


def _name(desc: str, payee: str | None) -> str:
    p = (payee or "").strip()
    return label(desc if not p or p.lower() in GENERIC_PAYEES else p)


def _flat(a: float, b: float) -> bool:
    return abs(a - b) <= max(FLAT_ABS, FLAT_PCT * max(a, b))


def fee_type(desc: str) -> tuple[str, str] | None:
    for t, lbl, rx in FEE_TYPES:
        if rx.search(desc or ""):
            return t, lbl
    return None


def _bill_re() -> re.Pattern | None:
    pats = (personal.section("subscriptions").get("bill_patterns") or [])
    return re.compile("|".join(f"(?:{p})" for p in pats), re.I) if pats else None


def category(desc: str, payee: str | None, kind: str, amount: float, extra: re.Pattern | None = None) -> str:
    text = f"{desc or ''} {payee or ''}"
    if PERSON_RE.search(text):
        return "person"
    if BILL_RE.search(text) or (extra and extra.search(text)) or (kind == "checking" and amount >= BILL_CHECKING_MIN):
        return "bill"
    return "subscription"


def _add_month(d: date) -> date:
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    raise AssertionError


def _spend(con, since: str) -> list[dict]:
    con.row_factory = sqlite3.Row
    rows = con.execute(f"""SELECT txn_id, date, amt, description, payee, account_name, kind, class, pending
                           FROM v_ledger WHERE kind IN ({','.join('?' * len(KINDS))}) AND amt < 0 AND date >= ?
                           ORDER BY date, txn_id""", (*KINDS, since)).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        r["d"] = date.fromisoformat(r["date"])
        r["a"] = round(-r["amt"], 2)
        r["key"] = merchant_key(r["description"], r["payee"])
        out.append(r)
    return out


def find_subscriptions(rows: list[dict], today: date) -> list[dict]:
    extra = _bill_re()
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["class"] == "spend" and not fee_type(r["description"]):
            by_key[r["key"]].append(r)
    subs, used = [], set()
    for cadence in ("monthly", "yearly"):  # monthly first: a yearly pairing inside a monthly series isn't another one
        gaps, expect, grace = CADENCES[cadence]
        for key, occ in by_key.items():
            pool = [r for r in occ if r["txn_id"] not in used]
            idx = {(r["d"], r["a"]): r for r in pool}
            for c in chains([(r["d"], r["a"]) for r in pool], MAX_RATIO, 2, gaps, expect):
                amts = [a for _, a in c]
                if cadence == "yearly":  # renewals: few of them, and the price often goes up each time
                    if max(amts) > YEARLY_RATIO * min(amts):
                        continue
                    varies = False
                else:
                    steps = [_flat(x, y) for x, y in zip(amts, amts[1:])]
                    varies = sum(steps) < len(steps) / 2
                    if varies and not (len(c) >= 3 and max(amts) <= VARIES_RATIO * min(amts)):
                        continue
                med = statistics.median(amts)
                dates = {d for d, _ in c}
                lo, hi = c[0][0] - timedelta(expect), c[-1][0] + timedelta(expect)  # one interval either side
                others = sum(1 for r in occ if r["d"] not in dates and lo <= r["d"] <= hi
                             and med / MAX_RATIO <= r["a"] <= med * MAX_RATIO)
                if others > len(c) // 2 or (len(c) == 2 and (varies or others)):
                    continue  # buried in similar purchases (top-ups, groceries), or too little to go on
                if cadence == "yearly" and sum(c[0][0] <= r["d"] <= c[-1][0] for r in occ) > YEARLY_BUSY:
                    continue  # a merchant used every week: two charges a year apart are a coincidence
                last_d, last_a = c[-1]
                nxt = _add_month(last_d) if cadence == "monthly" else (
                    last_d.replace(year=last_d.year + 1) if (last_d.month, last_d.day) != (2, 29)
                    else date(last_d.year + 1, 2, 28))
                if today > nxt + timedelta(grace):
                    continue  # stopped
                last = idx[(last_d, last_a)]
                for d, a in c:
                    used.add(idx[(d, a)]["txn_id"])
                recent = amts[-3:]
                base = statistics.median(recent) if varies else last_a
                first = c[0][0]
                earlier = any(r["d"] < first and med / MAX_RATIO <= r["a"] <= med * MAX_RATIO for r in occ)
                prev = amts[-2]
                up = not varies and last_a > prev and not _flat(last_a, prev)
                subs.append({
                    "key": key, "label": _name(last["description"], last["payee"]),
                    "description": last["description"], "payee": last["payee"], "account": last["account_name"],
                    "category": category(last["description"], last["payee"], last["kind"], last_a, extra),
                    "cadence": cadence, "count": len(c), "first_date": first.isoformat(),
                    "last_date": last_d.isoformat(), "last_amount": last_a, "varies": varies,
                    "next_date": nxt.isoformat(), "late": nxt < today,
                    "monthly": round(base if cadence == "monthly" else base / 12, 2),
                    "new": (today - first).days <= NEW_DAYS and not earlier,
                    "price_change": {"from": prev, "to": last_a, "pct": round(100 * (last_a / prev - 1), 1)}
                    if up else None,
                })
    subs.sort(key=lambda s: (-s["monthly"], s["label"]))
    return subs


def totals(subs: list[dict]) -> dict:
    out = {}
    for cat in ("subscription", "bill", "person", "all"):
        s = [x for x in subs if cat == "all" or x["category"] == cat]
        m = round(sum(x["monthly"] for x in s), 2)
        out[cat] = {"count": len(s), "monthly": m, "yearly": round(12 * m, 2)}
    return out


def find_unusual(rows: list[dict], today: date, window: int = WINDOW) -> list[dict]:
    start = today - timedelta(window)
    flagged: dict[str, dict] = {}

    def flag(r, kind, reason, rank, **extra):
        if r["txn_id"] in flagged and flagged[r["txn_id"]]["rank"] <= rank:
            return
        flagged[r["txn_id"]] = {"date": r["date"], "kind": kind, "reason": reason, "rank": rank,
                                "amount": -r["a"], "label": _name(r["description"], r["payee"]),
                                "description": r["description"], "payee": r["payee"], "account": r["account_name"],
                                "pending": bool(r["pending"]), **extra}

    recent = [r for r in rows if r["d"] >= start]
    for r in recent:
        ft = fee_type(r["description"]) if r["class"] in ("spend", "card_payment") else None
        if ft:
            flag(r, "fee", ft[1], 0, fee_type=ft[0])

    # transfers to people go to many recipients, so a "merchant history" means nothing for them
    spend = [r for r in rows if r["class"] == "spend" and not fee_type(r["description"])
             and not PERSON_RE.search(f"{r['description'] or ''} {r['payee'] or ''}")]
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in spend:
        by_key[r["key"]].append(r)
    for key, occ in by_key.items():
        groups: dict[float, list[dict]] = defaultdict(list)
        for r in occ:
            groups[r["a"]].append(r)
        for amt, same in groups.items():
            if amt < DUP_MIN:
                continue
            for a, b in zip(same, same[1:]):
                if b["d"] < start or (b["d"] - a["d"]).days > DUP_DAYS or a["pending"] != b["pending"]:
                    continue
                if sum(1 for r in same if r["d"] < a["d"] - timedelta(DUP_DAYS)) >= ROUTINE:
                    continue
                when = "same day" if a["d"] == b["d"] else f"also {a['d'].month}/{a['d'].day}"
                flag(b, "duplicate", f"Charged twice ({when})", 1, twin_date=a["date"])
        for i, r in enumerate(occ):
            if r["d"] < start or r["a"] <= BIG_MIN:
                continue
            hist = [h["a"] for h in occ[:i] if h["d"] < r["d"]]
            if len(hist) < BIG_HISTORY:
                continue
            med = statistics.median(hist)
            p95 = sorted(hist)[int(0.95 * (len(hist) - 1))]
            if r["a"] > BIG_FACTOR * med and r["a"] > p95:  # p95: skips merchants whose bills vary a lot anyway
                flag(r, "large", f"{r['a'] / med:.1f}x the usual ${med:,.0f} (median of {len(hist)})", 2,
                     usual=round(med, 2))
    out = sorted(flagged.values(), key=lambda x: x["date"], reverse=True)
    return sorted(out, key=lambda x: x["rank"])  # fees, then duplicates, then large; newest first within each


def report(con: sqlite3.Connection, today: date | None = None) -> dict:
    today = today or date.today()
    rows = _spend(con, (today - timedelta(LOOKBACK)).isoformat())
    subs = find_subscriptions(rows, today)
    unusual = find_unusual(rows, today)
    for u in unusual:
        u.pop("rank")
    return {"as_of": today.isoformat(), "subscriptions": subs, "totals": totals(subs), "unusual": unusual[:MAX_UNUSUAL],
            "unusual_count": len(unusual), "unusual_counts": {k: sum(u["kind"] == k for u in unusual)
                                                              for k in ("fee", "duplicate", "large")},
            "window_days": WINDOW, "new_days": NEW_DAYS}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args(argv)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    rep = report(con)
    if args.json:
        print(json.dumps(rep, indent=1))
        return 0
    t = rep["totals"]
    print(f"Recurring: subscriptions ${t['subscription']['monthly']:,.2f}/mo (${t['subscription']['yearly']:,.0f}/yr), "
          f"bills ${t['bill']['monthly']:,.2f}/mo, to people ${t['person']['monthly']:,.2f}/mo")
    for s in rep["subscriptions"]:
        flags = " ".join(f for f in ("NEW" if s["new"] else "", "LATE" if s["late"] else "",
                                     f"UP {s['price_change']['pct']:+.0f}%" if s["price_change"] else "") if f)
        print(f"  {s['category']:<12} {s['cadence']:<7} {s['label']:<32} ${s['last_amount']:>9,.2f} "
              f"last {s['last_date']} next {s['next_date']}  ${s['monthly']:,.2f}/mo {flags}")
    print(f"\nUnusual in the last {rep['window_days']} days ({rep['unusual_count']}):")
    for u in rep["unusual"]:
        print(f"  {u['date']}  {u['kind']:<9} {u['label']:<32} {u['amount']:>10,.2f}  {u['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
