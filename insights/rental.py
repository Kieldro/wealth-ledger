#!/usr/bin/env python3
"""Rental property P&L: booking payouts, tenant rent and the property's expenses, by month.

Which transactions belong to the rental is decided by LIKE patterns (case-insensitive, matched against the
description or the payee), first match wins:
  1. personal.json "rental.exclude"            never rental
  2. personal.json "rental.match"              {category: [pattern, ...]}, the owner's cleaners, utilities, ...
  3. DEFAULT_RULES below                       generic: booking-site payouts, host software
A pattern is a string, or {"pattern": "...", "direction": "in"|"out", "kinds": ["checking", ...]} to narrow it.
Other optional keys: "label" (section title), "start" (first day to count; default: dashboard.ledger_start),
"mortgage_account" (LIKE on account_name of the loan; default: the one loan account, preferring *MORTGAGE*),
"defaults": false to drop DEFAULT_RULES.

Income categories add the signed amount (a refund to a tenant lowers rent); expense categories add what was paid
out net of refunds. Only posted spend/income/refund rows count, so transfers and card payments never double count
(the mortgage counts once, from checking; card charges count where they were made).

Mortgage principal: the payment includes principal, which is equity, not a cost. It's measured from the loan
balance snapshots around each payment when there are snapshots on both sides; other payments are estimated from
the nearest measured one (scheduled principal changes only slightly month to month) plus any separately-labelled
principal on the loan account. Shown as "excluding principal" next to the cash flow.

  python -m insights.rental            monthly table
  python -m insights.rental --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

DB = Path(os.environ.get("WEALTH_LEDGER_DB") or Path.home() / ".local/share/wealth-ledger/txns.db")

INCOME = [("bookings", "Booking payouts"), ("rent", "Tenant rent"), ("other_income", "Other income")]
EXPENSES = [("mortgage", "Mortgage"), ("cleaning", "Cleaning"), ("software", "Software"), ("utilities", "Utilities"),
            ("insurance", "Insurance"), ("repairs", "Repairs & supplies"), ("other", "Other")]
INCOME_KEYS = {k for k, _ in INCOME}
CATEGORIES = {k for k, _ in INCOME + EXPENSES}
COUNTED = ("spend", "income", "refund")  # transfers, card payments and investments never count

# (category, LIKE pattern, direction, kinds). Generic only; anything naming the owner's people or providers belongs
# in personal.json "rental.match": tenants, cleaners, utilities, and the mortgage payment as your bank words it, e.g.
# "mortgage": [{"pattern": "%EXAMPLE HOME LOANS%", "direction": "out", "kinds": ["checking", "savings"]}].
HOST_SOFTWARE = ("GUESTY", "HOSPITABLE", "HOSTAWAY", "HOSTEX", "LODGIFY", "OWNERREZ", "PRICELABS")
DEFAULT_RULES = [
    ("bookings", "%AIRBNB%", "in", None),       # booking-site charges (out) are the owner's own stays, not the rental
    ("bookings", "%VRBO%", "in", None),
    *(("software", f"%{name}%", None, None) for name in HOST_SOFTWARE),
]
MATCH_WINDOW = 7      # days between the checking-side mortgage debit and the loan-side credit
SNAPSHOT_WINDOW = 10  # days after a loan payment to look for the balance that reflects it
BASIS_RANK = {"measured": 0, "estimated": 1, "labelled": 2, "partial": 3}


class Rule:
    def __init__(self, category, pattern, direction=None, kinds=None, source="default"):
        if category not in CATEGORIES:
            raise ValueError(f"rental: unknown category {category!r} (one of {', '.join(sorted(CATEGORIES))})")
        self.category, self.pattern, self.direction, self.source = category, pattern, direction, source
        self.kinds = tuple(kinds) if kinds else None
        self.rx = like_regex(pattern)

    def matches(self, r: dict) -> bool:
        if self.direction and self.direction != ("in" if r["amt"] > 0 else "out"):
            return False
        if self.kinds and r["kind"] not in self.kinds:
            return False
        return any(self.rx.fullmatch(t) for t in _texts(r))


def _texts(r: dict) -> tuple[str, str]:
    """Description and payee with runs of whitespace collapsed (banks pad names: "to PAT  TENANT")."""
    return " ".join((r["description"] or "").split()), " ".join((r["payee"] or "").split())


def like_regex(pattern: str) -> re.Pattern:
    """SQL LIKE (% and _) as a case-insensitive regex; whitespace runs in the pattern count as one space."""
    pattern = " ".join(pattern.split())
    return re.compile("".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern), re.I | re.S)


def _spec(p) -> dict:
    return {"pattern": p} if isinstance(p, str) else dict(p)


def rules(cfg: dict | None) -> tuple[list[re.Pattern], list[Rule]]:
    cfg = cfg or {}
    exclude = [like_regex(_spec(p)["pattern"]) for p in cfg.get("exclude") or []]
    out = []
    for cat, pats in (cfg.get("match") or {}).items():
        if cat.startswith("_"):
            continue
        for p in pats:
            s = _spec(p)
            out.append(Rule(cat, s["pattern"], s.get("direction"), s.get("kinds"), "personal"))
    if cfg.get("defaults", True):
        out += [Rule(*r) for r in DEFAULT_RULES]
    return exclude, out


def categorize(r: dict, exclude, rule_list) -> str | None:
    if any(x.fullmatch(t) for x in exclude for t in _texts(r)):
        return None
    return next((ru.category for ru in rule_list if ru.matches(r)), None)


# ---------- mortgage principal ----------
def _loan_account(con, cfg: dict) -> str | None:
    loans = [dict(r) for r in con.execute("SELECT account_id, account_name FROM v_accounts WHERE kind='loan'")]
    pat = cfg.get("mortgage_account")
    if pat:
        loans = [a for a in loans if like_regex(pat).fullmatch(a["account_name"] or "")]
    elif len(loans) > 1:
        loans = [a for a in loans if re.search("mortgage", a["account_name"] or "", re.I)] or loans
    return loans[0]["account_id"] if len(loans) == 1 else None


def loan_payments(con, loan_id: str) -> list[dict]:
    """Payments credited to the loan, one per date, with principal measured or estimated (see module doc)."""
    pays: dict[str, dict] = {}
    for r in con.execute("SELECT date, amt, description FROM v_ledger WHERE account_id=? AND amt>0 AND pending=0 "
                         "ORDER BY date", (loan_id,)):
        p = pays.setdefault(r["date"], {"date": r["date"], "paid": 0.0, "labelled": 0.0})
        p["paid"] += r["amt"]
        if re.search("principal", r["description"] or "", re.I):
            p["labelled"] += r["amt"]
    snaps = [(d, -(b or 0.0)) for d, b in con.execute(
        "SELECT snapshot_date, CAST(balance AS REAL) FROM balances WHERE account_id=? ORDER BY snapshot_date",
        (loan_id,))]
    out = sorted(pays.values(), key=lambda p: p["date"])
    for i, p in enumerate(out):
        L = date.fromisoformat(p["date"])
        prev = out[i - 1]["date"] if i else None
        nxt = out[i + 1]["date"] if i + 1 < len(out) else None
        before = [s for s in snaps if s[0] < p["date"] and (prev is None or s[0] > prev)]
        end = (L + timedelta(SNAPSHOT_WINDOW)).isoformat()
        p["measured"] = None
        if before:
            owed0 = before[-1][1]
            after = [s for s in snaps if p["date"] <= s[0] <= end and (nxt is None or s[0] < nxt)
                     and abs(s[1] - owed0) >= 0.005]
            if after and 0 < owed0 - after[0][1] <= p["paid"] + 0.005:
                p["measured"] = round(owed0 - after[0][1], 2)
    measured = [p for p in out if p["measured"] is not None]
    for p in out:
        if p["measured"] is not None:
            p["principal"], p["basis"] = p["measured"], "measured"
        elif measured:
            near = min(measured, key=lambda m: abs((date.fromisoformat(m["date"]) - date.fromisoformat(p["date"])).days))
            sched = max(0.0, near["measured"] - near["labelled"])
            p["principal"], p["basis"] = round(min(p["paid"], sched + p["labelled"]), 2), "estimated"
        elif p["labelled"]:
            p["principal"], p["basis"] = round(p["labelled"], 2), "labelled"
        else:
            p["principal"], p["basis"] = None, None
    return out


def _attach_principal(rows: list[dict], pays: list[dict]):
    """Give each checking-side mortgage debit the principal of the loan credit it paid (nearest within a week)."""
    free = list(pays)
    for r in sorted((r for r in rows if r["category"] == "mortgage" and r["amt"] < 0), key=lambda r: r["date"]):
        d = date.fromisoformat(r["date"])
        cands = [p for p in free if abs((date.fromisoformat(p["date"]) - d).days) <= MATCH_WINDOW]
        if not cands:
            continue
        p = min(cands, key=lambda p: abs((date.fromisoformat(p["date"]) - d).days))
        free.remove(p)
        if p["principal"] is not None:
            r["principal"] = round(min(p["principal"], -r["amt"]), 2)
            r["basis"] = p["basis"]


# ---------- aggregation ----------
def _months_back(d: date, n: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    m += 1
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(1)).day
    return date(y, m, min(d.day, last))


def _total(rows: list[dict]) -> dict:
    inc = defaultdict(float)
    exp = defaultdict(float)
    principal, bases, unknown = 0.0, set(), 0
    for r in rows:
        if r["category"] in INCOME_KEYS:
            inc[r["category"]] += r["amt"]
        else:
            exp[r["category"]] -= r["amt"]
        if r["category"] == "mortgage" and r["amt"] < 0:
            if r.get("principal") is None:
                unknown += 1
            else:
                principal += r["principal"]
                bases.add(r["basis"])
    income, expenses = sum(inc.values()), sum(exp.values())
    basis = None
    if bases or unknown:
        basis = "partial" if unknown else max(bases, key=BASIS_RANK.get)
    net = income - expenses
    return {"income": {k: round(v, 2) for k, v in inc.items()}, "expenses": {k: round(v, 2) for k, v in exp.items()},
            "income_total": round(income, 2), "expense_total": round(expenses, 2), "net": round(net, 2),
            "principal": round(principal, 2) if bases else None, "principal_basis": basis,
            "net_ex_principal": round(net + principal, 2) if bases else None, "n": len(rows)}


def summary(con: sqlite3.Connection, cfg: dict | None = None, start: str | None = None,
            today: date | None = None) -> dict:
    con.row_factory = sqlite3.Row
    cfg = cfg or {}
    today = today or date.today()
    start = cfg.get("start") or start or "1970-01-01"
    exclude, rule_list = rules(cfg)
    rows = []
    for r in con.execute(f"""SELECT date, amt, description, payee, account_name, kind, class FROM v_ledger
                             WHERE pending=0 AND date>=? AND date<=? AND class IN ({','.join('?' * len(COUNTED))})
                               AND COALESCE(kind,'') <> 'loan'
                             ORDER BY date, txn_id""", (start, today.isoformat(), *COUNTED)):
        r = dict(r)
        cat = categorize(r, exclude, rule_list)
        if cat:
            r["category"] = cat
            rows.append(r)
    loan = _loan_account(con, cfg)
    if loan:
        _attach_principal(rows, loan_payments(con, loan))

    first = date.fromisoformat(max(start, "1970-01-01"))
    months, m = [], first.replace(day=1)
    while m <= today:
        key = m.strftime("%Y-%m")
        t = _total([r for r in rows if r["date"][:7] == key])
        t.update(month=key, partial=(m < first) or (key == today.strftime("%Y-%m")),
                 from_day=first.day if key == first.strftime("%Y-%m") and first.day > 1 else None,
                 mtd=key == today.strftime("%Y-%m"))
        months.append(t)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)

    def period(since: date, label: str) -> dict:
        since = max(since, first)
        t = _total([r for r in rows if r["date"] >= since.isoformat()])
        t.update(label=label, since=since.isoformat(), through=today.isoformat(),
                 months=round(((today - since).days + 1) / (365.25 / 12), 2))
        return t

    present = {r["category"] for r in rows}
    items = [{"date": r["date"], "category": r["category"], "amt": round(r["amt"], 2),
              "what": (r["payee"] or r["description"] or "")[:60], "account": r["account_name"],
              "principal": r.get("principal")} for r in reversed(rows)]
    return {
        "label": cfg.get("label") or "Rental property",
        "since": first.isoformat(), "today": today.isoformat(),
        "income_categories": [{"key": k, "label": lbl} for k, lbl in INCOME if k in present],
        "expense_categories": [{"key": k, "label": lbl} for k, lbl in EXPENSES if k in present],
        "months": months,
        "periods": {"t3m": period(_months_back(today, 3) + timedelta(1), "Last 3 months"),
                    "ytd": period(date(today.year, 1, 1), f"{today.year} YTD")},
        "items": items,
        "rules": {"personal": sum(r.source == "personal" for r in rule_list), "default": sum(
            r.source == "default" for r in rule_list), "exclude": len(exclude)},
        "mortgage_account": bool(loan),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import personal
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    s = summary(con, personal.section("rental"), personal.section("dashboard").get("ledger_start"))
    if args.json:
        print(json.dumps(s, indent=1))
        return 0
    cols = [("income_total", "income"), ("expense_total", "expenses"), ("net", "net"), ("principal", "principal"),
            ("net_ex_principal", "ex-princ.")]
    print(f"{s['label']} since {s['since']}\n{'':10}" + "".join(f"{h:>11}" for _, h in cols))
    for row in s["months"] + list(s["periods"].values()):
        name = row.get("month") or row["label"]
        print(f"{name:10}" + "".join(f"{row[k]:>11,.0f}" if row[k] is not None else f"{'-':>11}" for k, _ in cols)
              + (f"  principal {row['principal_basis']}" if row["principal_basis"] not in (None, "measured") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
