#!/usr/bin/env python3
"""Overdraft check: will the scheduled auto-drafts overdraw a checking account in the next month?

Recurring debits and credits are found from history: the same description every 3-5 weeks (semi-monthly pay
shows up as two monthly series). They're projected HORIZON days ahead from each checking account's latest balance.
Card autopays come from the card side, where every attempt shows even when the bank bounced it. Each one is sized
as the balance owed at the statement close minus payments made since. The close-to-payment lag is learned per card
from which day's balance its past autopays matched (STATEMENT_LAG when none match). The draft is charged to the
checking account that paid that card before.
Rough by design: bills use their largest recent amount, pay its smallest; pay needs 3 occurrences and is dropped
once it misses its date.

  python -m insights.overdraft            forecast for every checking account
  python -m insights.overdraft --status   warning lines only (wealth-ledger-sync puts them in the status file)
  python -m insights.overdraft --json
Never sends notifications (owner's rule): warnings go to the status file, which the dashboard and the finances
skill read.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

DB = Path(os.environ.get("WEALTH_LEDGER_DB") or Path.home() / ".local/share/wealth-ledger/txns.db")
HORIZON = 31        # days ahead to project
BUFFER = 250.0      # warn when the projected low point drops under this
GRACE = 3           # a draft expected up to this many days ago that hasn't posted yet is treated as due today
STATEMENT_LAG = 21  # days from statement close to autopay when a card's own history can't tell (US minimum)
MIN_GAP, MAX_GAP = 21, 38  # spacing that counts as "monthly": 4-week card cycles up to weekend/holiday shifts
LOOKBACK = 120      # days of history to learn the series from
MIN_AMT = 5.0
MAX_RATIO = 1.33    # checking-side series: consecutive amounts within this factor (keeps bonus pay out of payroll)
SWEEPS = ("%CORE ACCOUNT%", "REINVESTMENT%")  # brokerage cash sweeps: internal, already inside the balance


def series_key(desc: str) -> str:
    d = re.sub(r"AUTHID:\S*", " ", (desc or "").upper())
    d = re.sub(r"\d+", "#", d)
    return " ".join(re.sub(r"[^A-Z# ]+", " ", d).split())


def label(desc: str) -> str:
    d = re.sub(r"^(DIRECT DEBIT|DIRECT DEPOSIT|DEBIT CARD PURCHASE)\s+|\s*\(Cash\)$|\s+AUTHID:\S*", "", desc or "")
    return " ".join(d.split())[:40]


def _gaps(chain) -> list[int]:
    return [(b[0] - a[0]).days for a, b in zip(chain, chain[1:])]


def chains(occ: list[tuple[date, float]], max_ratio: float | None = None, min_len: int = 2,
           gaps: tuple[int, int] = (MIN_GAP, MAX_GAP), expect_gap: float = 30) -> list[list[tuple[date, float]]]:
    """Split one description's occurrences into monthly series (e.g. two Amex autopays a few days apart).
    `gaps`/`expect_gap` set the spacing that links two occurrences (insights.subscriptions uses a yearly one)."""
    out: list[list] = []
    for d, a in sorted(occ):
        best, best_score = None, None
        for c in out:
            gap = (d - c[-1][0]).days
            ratio = max(abs(a), 0.01) / max(abs(c[-1][1]), 0.01)
            if gaps[0] <= gap <= gaps[1] and (max_ratio is None or 1 / max_ratio <= ratio <= max_ratio):
                expect = statistics.median(_gaps(c)) if len(c) > 1 else expect_gap
                score = abs(gap - expect) + 5 * abs(math.log(ratio))
                if best_score is None or score < best_score:
                    best, best_score = c, score
        if best is None:
            out.append([(d, a)])
        else:
            best.append((d, a))
    return [c for c in out if len(c) >= min_len]


def upcoming(chain, today: date, end: date, income: bool) -> list[date]:
    """Expected dates in [today, end]. A draft that is a few days late counts as due today."""
    interval = max(MIN_GAP, round(statistics.median(_gaps(chain))))
    last = chain[-1][0]
    if (today - last).days > 2 * interval + GRACE:
        return []  # stopped
    nxt = last + timedelta(interval)
    if income and nxt < today - timedelta(GRACE):
        return []  # pay that already missed its date isn't counted on
    while nxt < today - timedelta(GRACE):
        nxt += timedelta(interval)
    out = []
    while nxt <= end:
        out.append(max(nxt, today))
        nxt += timedelta(interval)
    return out


def _rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args)]


def _owed_at(con, card: dict, d: date) -> float:
    """Balance owed on a card at the end of day d, rebuilt from today's balance and the transactions since."""
    after = con.execute("SELECT COALESCE(SUM(amt),0) FROM v_ledger WHERE account_id=? AND pending=0 AND date>?",
                        (card["account_id"], d.isoformat())).fetchone()[0]
    return -(card["balance"] - after)


def _paid_between(con, card: dict, d: date, before: date | None = None) -> float:
    """Payments credited after d (and before `before`), net of bounced ones."""
    return con.execute("SELECT COALESCE(SUM(amt),0) FROM v_ledger WHERE account_id=? AND class='card_payment' "
                       "AND pending=0 AND date>? AND date<?",
                       (card["account_id"], d.isoformat(), (before or date.max).isoformat())).fetchone()[0]


def statement_lag(con, card: dict, pays: list[tuple[date, float]]) -> int:
    """Days from statement close to autopay, from the close date whose balance each past payment matched."""
    lags = []
    for pd, amt in pays[-6:]:
        fits = [L for L in range(3, 41)
                if abs(_owed_at(con, card, pd - timedelta(L)) - _paid_between(con, card, pd - timedelta(L), pd)
                       - amt) <= max(1.0, 0.02 * amt)]
        if fits:
            lags.append(min(fits))  # a run of equal fits = days with no new charges; the latest close is safest
    return round(statistics.median(lags)) if lags else STATEMENT_LAG


def _funding_account(con, card_id: str) -> str | None:
    """The checking account whose debits match this card's payments (same amount, within 5 days)."""
    r = con.execute("""
        SELECT l.account_id FROM v_ledger c JOIN v_ledger l
          ON l.kind='checking' AND l.amt<0 AND ABS(l.amt + c.amt) < 0.005
         AND ABS(julianday(l.date) - julianday(c.date)) <= 5
        WHERE c.account_id=? AND c.class='card_payment' AND c.amt>0
        GROUP BY l.account_id ORDER BY COUNT(*) DESC LIMIT 1""", (card_id,)).fetchone()
    return r[0] if r else None


def forecast(con: sqlite3.Connection, today: date | None = None, horizon: int = HORIZON) -> list[dict]:
    con.row_factory = sqlite3.Row
    today = today or date.today()
    end = today + timedelta(horizon)
    since = (today - timedelta(LOOKBACK)).isoformat()
    accounts = {a["account_id"]: dict(a, events=[]) for a in _rows(
        con, "SELECT account_id, account_name, balance, as_of FROM v_accounts WHERE kind='checking'")}

    # bills, pay and transfers on each checking account (card payments come from the card side below)
    sweep = " AND ".join("description NOT LIKE ?" for _ in SWEEPS)
    groups: dict[tuple, list] = defaultdict(list)
    for r in _rows(con, f"""SELECT account_id, date, amt, description FROM v_ledger
                            WHERE kind='checking' AND date>=? AND ABS(amt)>=? AND {sweep}
                              AND NOT (class='card_payment' AND amt<0)""", (since, MIN_AMT, *SWEEPS)):
        groups[(r["account_id"], series_key(r["description"]), r["amt"] > 0)].append(
            (date.fromisoformat(r["date"]), r["amt"], r["description"]))
    for (acct, _, income), occ in groups.items():
        if acct not in accounts:
            continue
        name = label(max(occ)[2])
        for c in chains([(d, a) for d, a, _ in occ], MAX_RATIO, min_len=3 if income else 2):
            recent = [a for _, a in c[-3:]]
            amt = min(recent)  # pay: smallest recent; bills (negative): largest
            for d in upcoming(c, today, end, income):
                accounts[acct]["events"].append({"date": d.isoformat(), "label": name, "amount": round(amt, 2),
                                                 "estimate": len(set(recent)) > 1})

    # card autopays, sized from the card's own statement estimate
    for card in _rows(con, "SELECT account_id, account_name, balance FROM v_accounts WHERE kind='credit'"):
        pays = [(date.fromisoformat(r["date"]), r["amt"]) for r in _rows(
            con, "SELECT date, amt FROM v_ledger WHERE account_id=? AND class='card_payment' AND amt>0 "
                 "AND pending=0 AND date>=?", (card["account_id"], since))]
        series = chains(pays)
        if not series:
            continue
        fund = _funding_account(con, card["account_id"])
        if fund not in accounts:
            continue
        lag = statement_lag(con, card, max(series, key=len))
        owed_now, planned = -(card["balance"] or 0), 0.0
        for c in series:
            for d in upcoming(c, today, end, income=False):
                close = d - timedelta(lag)
                if planned == 0 and close < today:
                    amt = _owed_at(con, card, close) - _paid_between(con, card, close)
                else:  # later cycle: whatever is owed now beyond what's already planned
                    amt = owed_now - planned
                amt = max(0.0, min(amt, max(owed_now, 0.0)))
                planned += amt
                if amt >= MIN_AMT:
                    accounts[fund]["events"].append({"date": d.isoformat(), "label": f"{card['account_name']} autopay",
                                                     "amount": -round(amt, 2), "estimate": True})

    out = []
    for a in accounts.values():
        ev = sorted(a["events"], key=lambda e: (e["date"], e["amount"] > 0))
        bal = a["balance"] or 0.0
        low, low_date, first_short = bal, None, None
        for e in ev:
            bal += e["amount"]
            e["balance_after"] = round(bal, 2)
            if bal < low:
                low, low_date = bal, e["date"]
            if bal < 0 and first_short is None:
                first_short = e["date"]
        out.append({"account_id": a["account_id"], "account": a["account_name"], "balance": a["balance"],
                    "as_of": a["as_of"], "low": round(low, 2), "low_date": low_date, "first_short": first_short,
                    "horizon_end": end.isoformat(), "events": ev})
    return out


def _usd(x: float) -> str:
    return f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


def _md(iso: str) -> str:
    return f"{int(iso[5:7])}/{int(iso[8:10])}"


def warnings(fc: list[dict], buffer: float = BUFFER) -> list[str]:
    """One status-file line per checking account whose projected balance dips under the buffer."""
    lines = []
    for a in fc:
        if a["low"] >= buffer or not a["low_date"]:  # nothing scheduled: a small balance alone isn't a risk
            continue
        upto = a["first_short"] or a["low_date"]
        big = sorted((e for e in a["events"] if e["amount"] < 0 and e["date"] <= upto), key=lambda e: e["amount"])[:3]
        drafts = ", ".join(f"{e['label']} ~${-e['amount']:,.0f} {_md(e['date'])}" for e in big)
        if a["first_short"]:
            lines.append(f"Overdraft risk: {a['account']} has {_usd(a['balance'])}, projected {_usd(a['low'])} by "
                         f"{_md(a['low_date'])} ({drafts}). Move money in before {_md(a['first_short'])}.")
        else:
            lines.append(f"Low balance: {a['account']} projected {_usd(a['low'])} on {_md(a['low_date'])} "
                         f"(under {_usd(buffer)}; {drafts}).")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--status", action="store_true", help="print only the warning lines")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--days", type=int, default=HORIZON)
    args = ap.parse_args(argv)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    fc = forecast(con, horizon=args.days)
    if args.json:
        print(json.dumps({"forecast": fc, "warnings": warnings(fc)}, indent=1))
    elif args.status:
        print("\n".join(warnings(fc)))
    else:
        for a in fc:
            print(f"\n{a['account']}: ${a['balance']:,.2f} (as of {(a['as_of'] or '')[:10]}), "
                  f"low ${a['low']:,.2f}{' on ' + a['low_date'] if a['low_date'] else ''}")
            for e in a["events"]:
                print(f"  {e['date']}  {e['amount']:>10,.2f}  {e['balance_after']:>10,.2f}  "
                      f"{e['label']}{' (est.)' if e['estimate'] else ''}")
        for w in warnings(fc):
            print("\n" + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
