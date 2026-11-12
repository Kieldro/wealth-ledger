#!/usr/bin/env python3
"""Overdraft check: will the scheduled auto-drafts overdraw a checking account in the next month?

Recurring debits and credits are found from history: the same description every 3-5 weeks (semi-monthly pay
shows up as two monthly series). They're projected HORIZON days ahead from each checking account's latest balance.
Card autopays come from the card side, where every attempt shows even when the bank bounced it. Each one is sized
as the balance owed at the statement close minus payments made since, or, while that statement is still open, what
is owed now plus the card's recent daily charges until it closes. The close-to-payment lag is learned per card:
the lag that explains the most of its past autopays (STATEMENT_LAG when none match). The draft is charged to the
checking account that paid that card before.
Rough by design: bills use their largest recent amount; pay uses its smallest recent amount, or the latest one once
the payer's last two deposits agree; pay needs 3 occurrences and is dropped once it misses its date. A bill seen
three times that misses a cycle is still projected for the next one, flagged "missed last cycle": it may have been
paid another way that month (rent by check) or have ended, and the history can't tell which, so the forecast errs
on the side of the debit. After two missed cycles, or with fewer than three occurrences, it is dropped. A bill
with a known last day goes in personal.json "overdraft": {"ends": {"<description LIKE pattern>": "YYYY-MM-DD"}}:
nothing is projected for it after that date. A bill that resumes on its usual day is one series again. A card autopay survives one missed cycle: issuers skip a draft when the statement was
already paid by hand, and resume. Frequent deposits with no monthly rhythm (many small payouts from one payer) are
never projected; they're listed per account as `uncounted_income` and named in the warning.

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import personal  # noqa: E402

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
ESTABLISHED = 3     # a bill seen this many times is still expected after one missed cycle
MISSED_NOTE = "missed last cycle"
SKIP_TOL = 4        # a gap counts as "one period skipped" when half of it is within this many days of the usual spacing
SAME_PAY = 0.01     # a payer's last two deposits "agree" within this fraction
SPEND_DAYS = 60     # a card's recent charge rate is taken over this many days
UNCOUNTED_DAYS, UNCOUNTED_MIN = 60, 3  # unprojected income worth naming: this many deposits from one payer in the window


def series_key(desc: str) -> str:
    d = re.sub(r"AUTHID:\S*", " ", (desc or "").upper())
    d = re.sub(r"\d+", "#", d)
    return " ".join(re.sub(r"[^A-Z# ]+", " ", d).split())


def label(desc: str) -> str:
    d = re.sub(r"^(DIRECT DEBIT|DIRECT DEPOSIT|DEBIT CARD PURCHASE)\s+|\s*\(Cash\)$|\s+AUTHID:\S*", "", desc or "")
    return " ".join(d.split())[:40]


def _gaps(chain, max_gap: int = MAX_GAP) -> list[float]:
    """Days between occurrences, per period: a gap too long for one period skipped one, so it counts as two halves."""
    days = [(b[0] - a[0]).days for a, b in zip(chain, chain[1:])]
    return [g / 2 if g > max_gap else g for g in days]


def skipped(chain, max_gap: int = MAX_GAP) -> bool:
    """True when the series missed a period somewhere (see `chains(skips=1)`)."""
    return any((b[0] - a[0]).days > max_gap for a, b in zip(chain, chain[1:]))


def chains(occ: list[tuple[date, float]], max_ratio: float | None = None, min_len: int = 2,
           gaps: tuple[int, int] = (MIN_GAP, MAX_GAP), expect_gap: float = 30,
           skips: int = 0) -> list[list[tuple[date, float]]]:
    """Split one description's occurrences into monthly series (e.g. two Amex autopays a few days apart).
    `gaps`/`expect_gap` set the spacing that links two occurrences (insights.subscriptions uses a yearly one).
    With `skips=1` a series that already has two occurrences may miss one period (rent paid another way for a
    month, an autopay the issuer skipped) and pick up again, if it resumes within SKIP_TOL days of its usual day."""
    out: list[list] = []
    for d, a in sorted(occ):
        best, best_score = None, None
        for c in out:
            gap = (d - c[-1][0]).days
            ratio = max(abs(a), 0.01) / max(abs(c[-1][1]), 0.01)
            if max_ratio is not None and not 1 / max_ratio <= ratio <= max_ratio:
                continue
            expect = statistics.median(_gaps(c, gaps[1])) if len(c) > 1 else expect_gap
            if gaps[0] <= gap <= gaps[1]:
                score = abs(gap - expect) + 5 * abs(math.log(ratio))
            elif skips and len(c) > 1 and not skipped(c, gaps[1]) and abs(gap / 2 - expect) <= SKIP_TOL:
                score = gaps[1] + abs(gap / 2 - expect) + 5 * abs(math.log(ratio))  # any on-time link beats it
            else:
                continue
            if best_score is None or score < best_score:
                best, best_score = c, score
        if best is None:
            out.append([(d, a)])
        else:
            best.append((d, a))
    return [c for c in out if len(c) >= min_len]


def upcoming(chain, today: date, end: date, income: bool, missed: int = 0) -> list[date]:
    """Expected dates in [today, end]. A draft that is a few days late counts as due today. `missed` is how many
    whole cycles may pass without the draft before the series counts as stopped."""
    interval = max(MIN_GAP, round(statistics.median(_gaps(chain))))
    last = chain[-1][0]
    if (today - last).days > (1 + missed) * max(interval, MAX_GAP) + GRACE:
        return []  # stopped, or skipped a whole cycle (e.g. rent paid by cashier's check instead of the auto-debit)
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


def missed_cycle(chain, today: date) -> bool:
    """True when the series' last occurrence is more than one cycle back (see `upcoming(missed=1)`)."""
    interval = max(MIN_GAP, round(statistics.median(_gaps(chain))))
    return (today - chain[-1][0]).days > max(interval, MAX_GAP) + GRACE


def bill_ends(con, description: str, ends: dict[str, str]) -> date | None:
    """The last day a bill can be drafted, from personal.json "overdraft.ends" (description LIKE pattern -> date)."""
    hits = [date.fromisoformat(d) for pat, d in ends.items()
            if not pat.startswith("_") and con.execute("SELECT ? LIKE ?", (description, pat)).fetchone()[0]]
    return min(hits) if hits else None


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


def _daily_charges(con, card: dict, today: date) -> float:
    """What the card has been charged per day lately (purchases net of refunds, no payments)."""
    since = today - timedelta(SPEND_DAYS)
    net = con.execute("SELECT COALESCE(SUM(amt),0) FROM v_ledger WHERE account_id=? AND class<>'card_payment' "
                      "AND pending=0 AND date>? AND date<=?",
                      (card["account_id"], since.isoformat(), today.isoformat())).fetchone()[0]
    return max(0.0, -net / SPEND_DAYS)


def statement_lag(con, card: dict, pays: list[tuple[date, float]]) -> int:
    """Days from statement close to autopay, learned from the closes the card's past autopays fit.

    Each payment fits a run of lags (the days around its close with no new charges), and a payment that wasn't
    the statement balance (made by hand mid-cycle, or a partial one) fits unrelated ones. So every payment votes
    for all the lags it fits. The most-voted lags win, together with any lag only one payment short of them once
    three agree: closes move a day either way between cycles, so "all but one" is as good as it gets. Among
    those, the lag nearest the usual STATEMENT_LAG, then the shorter (a later close counts more charges: the
    safer side). Expect the result to be a day's charges off now and then."""
    votes: dict[int, int] = defaultdict(int)
    for pd, amt in pays[-6:]:
        for L in range(3, 41):
            close = pd - timedelta(L)
            if abs(_owed_at(con, card, close) - _paid_between(con, card, close, pd) - amt) <= max(1.0, 0.02 * amt):
                votes[L] += 1
    if not votes:
        return STATEMENT_LAG
    top = max(votes.values())
    good = [L for L, n in votes.items() if n >= (top - 1 if top >= 3 else top)]
    return min(good, key=lambda L: (abs(L - STATEMENT_LAG), L))


def _funding_account(con, card_id: str) -> str | None:
    """The checking account whose debits match this card's payments (same amount, within 5 days)."""
    r = con.execute("""
        SELECT l.account_id FROM v_ledger c JOIN v_ledger l
          ON l.kind='checking' AND l.amt<0 AND ABS(l.amt + c.amt) < 0.005
         AND ABS(julianday(l.date) - julianday(c.date)) <= 5
        WHERE c.account_id=? AND c.class='card_payment' AND c.amt>0
        GROUP BY l.account_id ORDER BY COUNT(*) DESC LIMIT 1""", (card_id,)).fetchone()
    return r[0] if r else None


def forecast(con: sqlite3.Connection, today: date | None = None, horizon: int = HORIZON,
             ends: dict[str, str] | None = None) -> list[dict]:
    if ends is None:
        try:
            ends = personal.section("overdraft").get("ends") or {}
        except personal.PersonalConfigError:  # no personal.json: nothing is known to end
            ends = {}
    con.row_factory = sqlite3.Row
    today = today or date.today()
    end = today + timedelta(horizon)
    since = (today - timedelta(LOOKBACK)).isoformat()
    accounts = {a["account_id"]: dict(a, events=[], uncounted=[]) for a in _rows(
        con, "SELECT account_id, account_name, balance, as_of FROM v_accounts WHERE kind='checking'")}

    # bills, pay and transfers on each checking account (card payments come from the card side below)
    sweep = " AND ".join("description NOT LIKE ?" for _ in SWEEPS)
    groups: dict[tuple, list] = defaultdict(list)
    for r in _rows(con, f"""SELECT account_id, date, amt, description, class FROM v_ledger
                            WHERE kind='checking' AND date>=? AND ABS(amt)>=? AND {sweep}
                              AND NOT (class='card_payment' AND amt<0)""", (since, MIN_AMT, *SWEEPS)):
        groups[(r["account_id"], series_key(r["description"]), r["amt"] > 0)].append(
            (date.fromisoformat(r["date"]), r["amt"], r["description"], r["class"]))
    window = today - timedelta(UNCOUNTED_DAYS)
    for (acct, _, income), occ in groups.items():
        if acct not in accounts:
            continue
        name = label(max(occ)[2])
        series = chains([(d, a) for d, a, *_ in occ], MAX_RATIO, min_len=3 if income else 2, skips=0 if income else 1)
        # pay: once the payer's last two regular deposits agree, that is the going rate (a raise, or the end of a
        # short month); until then the smallest recent one. Bills (negative): always the largest recent one.
        last2 = [a for _, a in sorted(p for c in series for p in c)[-2:]] if income else []
        settled = len(last2) == 2 and abs(last2[0] - last2[1]) <= SAME_PAY * max(last2)
        projected = False
        last_day = None if income else bill_ends(con, max(occ)[2], ends)
        for c in series:
            recent = [a for _, a in c[-3:]]
            amt = last2[-1] if settled else min(recent)
            # an established bill that just missed a cycle: paid another way this once, or over? Keep it, flagged.
            missed = not income and len(c) >= ESTABLISHED and missed_cycle(c, today)
            for d in upcoming(c, today, end, income, missed=1 if missed else 0):
                if last_day and d > last_day:
                    continue
                projected = True
                ev = {"date": d.isoformat(), "label": name, "amount": round(amt, 2),
                      "estimate": len(set(recent)) > 1 or skipped(c) or missed}
                if missed:
                    ev["note"] = MISSED_NOTE
                accounts[acct]["events"].append(ev)
        late = [a for d, a, _, cls in occ if d >= window and cls == "income"]
        if income and not projected and len(late) >= UNCOUNTED_MIN:
            accounts[acct]["uncounted"].append({"label": name, "count": len(late), "days": UNCOUNTED_DAYS,
                                                "monthly": round(sum(late) * 30 / UNCOUNTED_DAYS, 2)})

    # card autopays, sized from the card's own statement estimate
    for card in _rows(con, "SELECT account_id, account_name, balance FROM v_accounts WHERE kind='credit'"):
        pays = [(date.fromisoformat(r["date"]), r["amt"]) for r in _rows(
            con, "SELECT date, amt FROM v_ledger WHERE account_id=? AND class='card_payment' AND amt>0 "
                 "AND pending=0 AND date>=?", (card["account_id"], since))]
        series = chains(pays, skips=1)
        if not series:
            continue
        fund = _funding_account(con, card["account_id"])
        if fund not in accounts:
            continue
        lag = statement_lag(con, card, max(series, key=len))
        owed_now, planned = -(card["balance"] or 0), 0.0
        for c in series:
            for d in upcoming(c, today, end, income=False, missed=1):
                close = d - timedelta(lag)
                cap = max(owed_now, 0.0)
                if planned == 0 and close < today:
                    amt = _owed_at(con, card, close) - _paid_between(con, card, close)
                elif planned == 0:  # statement still open: owed now plus the usual charges until it closes
                    cap += _daily_charges(con, card, today) * (close - today).days
                    amt = cap
                else:  # later cycle: whatever is owed now beyond what's already planned
                    amt = owed_now - planned
                amt = max(0.0, min(amt, cap))
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
                    "horizon_end": end.isoformat(), "events": ev,
                    "uncounted_income": sorted(a["uncounted"], key=lambda u: -u["monthly"])})
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
        drafts = ", ".join(f"{e['label']} ~${-e['amount']:,.0f} {_md(e['date'])}"
                           f"{', ' + e['note'] if e.get('note') else ''}" for e in big)
        if a["first_short"]:
            line = (f"Overdraft risk: {a['account']} has {_usd(a['balance'])}, projected {_usd(a['low'])} by "
                    f"{_md(a['low_date'])} ({drafts}). Move money in before {_md(a['first_short'])}.")
        else:
            line = (f"Low balance: {a['account']} projected {_usd(a['low'])} on {_md(a['low_date'])} "
                    f"(under {_usd(buffer)}; {drafts}).")
        lines.append(line + _uncounted_note(a))
    return lines


def _uncounted_note(a: dict) -> str:
    """What the projection leaves out on the income side, so a warning isn't read as the whole picture."""
    unc = a.get("uncounted_income") or []
    if not unc:
        return ""
    names = ", ".join(f"{u['label']} ~{_usd(u['monthly'])}/mo" for u in unc[:2])
    return f" Not counted: irregular deposits ({names}{', ...' if len(unc) > 2 else ''})."


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
                      f"{e['label']}{' (est.)' if e['estimate'] else ''}{' (' + e['note'] + ')' if e.get('note') else ''}")
            for u in a["uncounted_income"]:
                print(f"  not counted: {u['label']}, {u['count']} irregular deposits in {u['days']} days "
                      f"(~{_usd(u['monthly'])}/mo)")
        for w in warnings(fc):
            print("\n" + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
