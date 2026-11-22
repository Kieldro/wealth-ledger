"""When the mortgage is paid off, from the ledger alone: the loan account's balance, the payments that go into it each
month (extra principal included) and the interest rate those two imply.

  balance  the latest synced balance of the loan account named like "mortgage" (else the largest loan)
  payment  the median of the last few months' totals paid into that account (every credit to it in a month: the
           regular payment and any extra principal)
  rate     from the balance drops around each payment: interest = what was paid - how much the balance fell, over the
           balance before; the median of the months that have a balance on both sides. A % in the account's name
           (e.g. "mortgage 6.75%") stands in when no month has both sides.
  payoff   the balance amortized at that payment and rate, month by month from the month after the last payment

The dashboard's Liabilities tile says "Paid off Sep 2035 at $1,600/mo · 6.75%". Reads the ledger only; writes nothing.
"""
from __future__ import annotations

import math
import re
import sqlite3
from datetime import date
from statistics import median

MONTHS = 6            # payment months looked at
RATE_RANGE = (0.002, 0.25)  # plausible yearly rates; anything else is a derivation gone wrong


def add_months(month: str, n: int) -> str:
    """'2026-11' + 3 -> '2027-02'."""
    y, m = map(int, month[:7].split("-"))
    k = y * 12 + m - 1 + n
    return f"{k // 12:04d}-{k % 12 + 1:02d}"


def months_to_pay(balance: float, payment: float, rate_m: float) -> int | None:
    """Payments of `payment` a month that clear `balance` at `rate_m` a month (the last one smaller); None when the
    payment doesn't even cover the interest."""
    if balance <= 0:
        return 0
    if payment <= 0:
        return None
    if rate_m <= 0:
        return math.ceil(balance / payment)
    if rate_m * balance >= payment:
        return None
    return math.ceil(-math.log(1 - rate_m * balance / payment) / math.log(1 + rate_m) - 1e-9)


def _loan(con: sqlite3.Connection) -> tuple[str, str, float] | None:
    rows = con.execute("""SELECT b.account_id, b.account_name, CAST(b.balance AS REAL) FROM v_balances_latest b
                          JOIN account_kind k USING (account_id) WHERE k.kind = 'loan'""").fetchall()
    rows = [r for r in rows if (r[2] or 0) < -0.005]
    named = [r for r in rows if re.search(r"mortgage", r[1] or "", re.I)]
    return min(named or rows, key=lambda r: r[2]) if rows else None


def summary(con: sqlite3.Connection, today: date | None = None, rate_hint: str = "") -> dict | None:
    """{account, balance, payment, rate (yearly, 0.0675), rate_from ("balance drops" | "name"), last_payment, payoff
    ("2035-09"), months} or None when there is no loan with payments to go on. rate_hint: other text that may name
    the rate (the balance-sheet line)."""
    today = today or date.today()
    loan = _loan(con)
    if not loan:
        return None
    aid, name, bal = loan
    balance = -bal
    pays = con.execute("""SELECT date(posted), CAST(amount AS REAL) FROM txns WHERE account_id = ? AND CAST(amount AS REAL) > 0
                          AND COALESCE(pending, 0) = 0 AND date(posted) <= ? ORDER BY 1""",
                       (aid, today.isoformat())).fetchall()
    by_month: dict[str, list] = {}
    for d, amt in pays:
        by_month.setdefault(d[:7], []).append((d, amt))
    months = sorted(by_month)[-MONTHS:]
    if not months:
        return None
    payment = round(median(sum(a for _, a in by_month[m]) for m in months), 2)
    snaps = con.execute("SELECT snapshot_date, CAST(balance AS REAL) FROM balances WHERE account_id = ? ORDER BY 1",
                        (aid,)).fetchall()
    rates = []
    for i, m in enumerate(months):
        first, paid = by_month[m][0][0], sum(a for _, a in by_month[m])
        nxt = by_month[months[i + 1]][0][0] if i + 1 < len(months) else "9999"
        before = [-b for d, b in snaps if d < first]
        after = [-b for d, b in snaps if first < d < nxt]
        if before and after and before[-1] > 0:
            r = (paid - (before[-1] - after[-1])) / before[-1]
            if RATE_RANGE[0] / 12 <= r <= RATE_RANGE[1] / 12:
                rates.append(r)
    rate_from = "balance drops"
    if rates:
        rate_m = median(rates)
    else:
        hit = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%", f"{name} {rate_hint}")
        if not hit or not RATE_RANGE[0] <= float(hit.group(1)) / 100 <= RATE_RANGE[1]:
            return None
        rate_m, rate_from = float(hit.group(1)) / 1200, "name"
    n = months_to_pay(balance, payment, rate_m)
    last = by_month[months[-1]][-1][0]
    return {"account": name, "balance": round(balance, 2), "payment": payment, "rate": round(rate_m * 12, 5),
            "rate_from": rate_from, "last_payment": last, "months": n,
            "payoff": add_months(last, n) if n is not None else None}
