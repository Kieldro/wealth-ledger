#!/usr/bin/env python3
"""Monthly summary email: last month's income, spending by category against the month before, balances, rental
P&L and anything odd. Everything comes from the ledger database; no spreadsheet is read, so it works for any
ledger with synced accounts.

Off unless personal.json has "monthly_email": {"enabled": true}. Sent with `gws` from the signed-in Gmail to
itself ("to" in that section names another address). The daily sync calls --if-due: one email per month, on or
after "day" (default 3, so late-posting charges are in), remembered in a state file so a missed day catches up.

Categories: [transaction regex on 'payee | description', category] pairs, first match wins: personal.json
"monthly_email.categories" first, then DEFAULT_CATEGORIES; the rest is "Other".

  python -m insights.monthly_email                    print last month's email (sends nothing)
  python -m insights.monthly_email --month 2026-10    another month
  python -m insights.monthly_email --html out.html    also write the HTML body to a file
  python -m insights.monthly_email --send             send it now
  python -m insights.monthly_email --if-due           what the sync runs
"""
from __future__ import annotations

import argparse
import base64
import calendar
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from email.message import EmailMessage
from html import escape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import personal  # noqa: E402
from insights import rental, subscriptions  # noqa: E402

DB = Path(os.environ.get("WEALTH_LEDGER_DB") or Path.home() / ".local/share/wealth-ledger/txns.db")
STATE = Path(os.environ.get("WEALTH_LEDGER_EMAIL_STATE") or Path.home() / ".local/state/wealth-ledger/monthly-email")
STATUS = Path(os.environ.get("WEALTH_LEDGER_STATUS") or Path.home() / ".cache/wealth-ledger-status")
SEND_DAY = 3
BALANCE_KINDS = ("credit", "retirement", "checking", "savings", "investment", "hsa", "loan")  # as networth.py
TOP = 5
OTHER = "Other"
DEFAULT_CATEGORIES = [  # generic merchants only; your own landlord, utilities and people go in personal.json
    (r"\bFEE\b|INTEREST CHARGE|INTEREST DEBIT|LATE CHARGE", "Fees & interest"),
    (r"ATM WITHDRAWAL|CASH ADVANCE", "Cash"),
    (r"RENT A CAR|CAR RENTAL|\bHERTZ\b|\bAVIS\b|ENTERPRISE RENT", "Travel"),
    (r"INSURANCE|HISCOX|GEICO|PROGRESSIVE INS|STATE FARM|ALLSTATE", "Insurance"),
    (r"SHELL|EXXON|CHEVRON|VALERO|BUC-?EE|\bGAS\b|\bFUEL\b|\bUBER\b(?!.*EATS)|\bLYFT\b|\bTOLL|EZ ?TAG|TXTAG|PARKING"
     r"|JIFFY LUBE|AUTOZONE|O'?REILLY AUTO|CAR ?WASH", "Car & transport"),
    (r"H-E-B|\bHEB\b|COSTCO|WHOLE FOODS|WHOLEFDS|TRADER JOE|KROGER|SAFEWAY|\bALDI\b|SPROUTS|CENTRAL MARKET|PUBLIX"
     r"|GROCER", "Groceries"),
    (r"RESTAURANT|GRILL|TAQUER|BURGER|PIZZA|\bCAFE\b|COFFEE|STARBUCKS|CHIPOTLE|WHATABURGER|CHICK-FIL-A|MCDONALD|TACO"
     r"|\bBBQ\b|SUSHI|RAMEN|DOORDASH|UBER ?EATS|GRUBHUB|DINER|CANTINA|BAKERY|DONUT", "Dining"),
    (r"MORTGAGE|\bRENT\b|APARTMENTS?\b|PROPERTY TAX|\bHOA\b|HOMEOWNERS", "Housing"),
    (r"ELECTRIC|ENERGY|\bWATER\b|UTILIT|SPECTRUM|XFINITY|COMCAST|GOOGLE FIBER|AT&T|T-?MOBILE|VERIZON|MINT MOBILE",
     "Utilities & phone"),
    (r"NETFLIX|SPOTIFY|YOUTUBE|GOOGLE ?ONE|HULU|DISNEY ?PLUS|APPLE\.COM/BILL|AMAZON PRIME|PRIME VIDEO|ANTHROPIC"
     r"|CLAUDE\.AI|OPENAI|PATREON|ICLOUD", "Subscriptions"),
    (r"AIRLINES?\b|AMERICAN AIR|UNITED AIR|DELTA AIR|SOUTHWEST|JETBLUE|AIRBNB|HOTEL|MARRIOTT|HILTON|HYATT|EXPEDIA"
     r"|BOOKING\.COM|VRBO", "Travel"),
    (r"PHARMAC|\bCVS\b|WALGREENS|DENTAL|DENTIST|MEDICAL|CLINIC|HOSPITAL|LABCORP|QUEST DIAG|\bGYM\b|FITNESS|WELLHUB"
     r"|YOGA|JIU|JITSU|\bHEALTH\b", "Health & fitness"),
    (r"AMAZON|AMZN|WAL-?MART|TARGET|BEST BUY|HOME DEPOT|LOWE'?S|EBAY|ETSY|IKEA", "Shopping"),
    (r"ZELLE|VENMO|CASH APP|REMITLY|WESTERN UNION", "People (Zelle, Venmo)"),
]


def label(desc: str | None, payee: str | None) -> str:
    s = (payee or "").strip() or (desc or "").strip()
    return re.sub(r"\s*[#*].*$", "", s).strip().title() or "(unknown)"


def month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def category_rules(own: list | None = None) -> list[tuple[re.Pattern, str]]:
    """personal.json monthly_email.categories (tried first) + DEFAULT_CATEGORIES, compiled."""
    return [(re.compile(t, re.I), c) for t, c in [tuple(r) for r in own or []] + DEFAULT_CATEGORIES]


def categorize(desc: str | None, payee: str | None, rule_list) -> str:
    text = f"{payee or ''} | {desc or ''}"
    return next((c for rx, c in rule_list if rx.search(text)), OTHER)


def last_month(today: date) -> str:
    return (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def balance_change(con: sqlite3.Connection, first: date, last: date) -> dict | None:
    """Change in synced balances over [first, last]: each account's last snapshot before `first` vs its last one
    on or before `last`. Only accounts with a snapshot on both sides count, so a newly linked account's opening
    balance is not a "gain". When the snapshots only begin inside the month, the change is measured from each
    account's first one and "since" says from when. None when there is nothing to compare."""
    rows = con.execute(f"""SELECT b.account_id, b.snapshot_date, CAST(b.balance AS REAL) FROM balances b
                           JOIN account_kind k USING (account_id)
                           WHERE k.kind IN ({','.join('?' * len(BALANCE_KINDS))}) AND b.snapshot_date <= ?
                             AND b.account_id NOT IN (SELECT account_id FROM superseded)
                           ORDER BY b.snapshot_date""", (*BALANCE_KINDS, last.isoformat())).fetchall()
    before, opening, end = {}, {}, {}
    for aid, d, bal in rows:
        if d < first.isoformat():
            before[aid] = bal or 0.0
        else:
            opening.setdefault(aid, (bal or 0.0, d))
        end[aid] = bal or 0.0
    since = None
    if not before and opening:  # history starts inside the month
        since = min(d for _, d in opening.values())
        before = {a: bal for a, (bal, d) in opening.items() if d == since}
        if since == rows[-1][1]:
            return None  # a single day of snapshots
    both = [a for a in end if a in before]
    if not both:
        return None
    return {"start": round(sum(before[a] for a in both), 2), "end": round(sum(end[a] for a in both), 2),
            "delta": round(sum(end[a] - before[a] for a in both), 2), "accounts": len(both),
            "new_accounts": len(end) - len(both), "since": since}


def _spend(con: sqlite3.Connection, first: date, last: date) -> list[dict]:
    return [dict(r) for r in con.execute(
        """SELECT date, amt, description, payee FROM v_ledger WHERE class='spend' AND pending=0
           AND date BETWEEN ? AND ? ORDER BY amt""", (first.isoformat(), last.isoformat()))]


def report(con: sqlite3.Connection, month: str, today: date | None = None, since: str | None = None,
           rental_cfg: dict | None = None, categories: list | None = None, problems: list[str] | None = None) -> dict:
    """Everything the email shows for `month` (YYYY-MM), read from the ledger only. since: first day of complete
    data; the month before is compared only when it is complete."""
    today = today or date.today()
    first, last = month_bounds(month)
    pfirst, plast = month_bounds((first - timedelta(days=1)).strftime("%Y-%m"))
    has_prev = not since or pfirst.isoformat() >= since
    con.row_factory = sqlite3.Row
    span = (first.isoformat(), last.isoformat())
    tot = {r["class"]: r["total"] for r in con.execute(
        """SELECT class, SUM(amt) total FROM v_ledger WHERE class IN ('spend','income') AND pending=0
           AND date BETWEEN ? AND ? GROUP BY class""", span)}
    income, spent = round(tot.get("income") or 0.0, 2), round(-(tot.get("spend") or 0.0), 2)

    rule_list = category_rules(categories)
    spend_rows, prev_rows = _spend(con, first, last), _spend(con, pfirst, plast) if has_prev else []
    cats: dict[str, list] = {}
    merchants: dict[str, list] = {}
    for rows, i in ((spend_rows, 0), (prev_rows, 1)):
        for r in rows:
            cats.setdefault(categorize(r["description"], r["payee"], rule_list), [0.0, 0.0])[i] -= r["amt"]
    for r in spend_rows:
        m = merchants.setdefault(label(r["description"], r["payee"]), [0.0, 0])
        m[0] -= r["amt"]
        m[1] += 1
    prev_spent = round(-sum(r["amt"] for r in prev_rows), 2) if has_prev else None

    rent = next((m for m in rental.summary(con, rental_cfg, since, today=min(today, last))["months"]
                 if m["month"] == month), None)
    rep = subscriptions.report(con, today=min(today, last + timedelta(days=1)))
    unusual = [{"date": u["date"], "label": u["label"], "amount": round(abs(u["amount"]), 2), "reason": u["reason"]}
               for u in rep["unusual"] if span[0] <= u["date"] <= span[1]]
    return {
        "month": month, "label": first.strftime("%B %Y"), "prev_label": pfirst.strftime("%B") if has_prev else None,
        "partial": bool(since and span[0] < since),
        "income": income, "spent": spent, "net": round(income - spent, 2), "prev_spent": prev_spent,
        "categories": [{"category": k, "spent": round(v[0], 2), "prev": round(v[1], 2) if has_prev else None}
                       for k, v in sorted(cats.items(), key=lambda kv: (-kv[1][0], -kv[1][1])) if max(v) >= 0.005],
        "top_merchants": [{"merchant": k, "spent": round(v[0], 2), "n": v[1]}
                          for k, v in sorted(merchants.items(), key=lambda kv: -kv[1][0])[:TOP]],
        "largest": [{"date": r["date"], "what": label(r["description"], r["payee"]),
                     "amount": round(-r["amt"], 2)} for r in spend_rows[:TOP]],
        "balances": balance_change(con, first, last),
        "rental": rent and rent["n"] and {"income": rent["income_total"], "expenses": rent["expense_total"],
                                          "net": rent["net"], "net_ex_principal": rent["net_ex_principal"]} or None,
        "unusual": unusual, "problems": problems or [],
    }


# ---------- rendering ----------
def money(v: float, sign: bool = False) -> str:
    return f"{'-' if v < 0 else '+' if sign else ''}${abs(v):,.0f}"


def sections(rep: dict) -> list[tuple[str, list[tuple[str, ...]]]]:
    """[(title, rows)], each row a tuple of cells; shared by the text and HTML bodies."""
    summary = [("Income", money(rep["income"])), ("Spent", money(rep["spent"])), ("Net", money(rep["net"], sign=True))]
    if rep["prev_spent"] is not None:
        summary.append((f"Spent in {rep['prev_label']}", money(rep["prev_spent"])))
    out = [("Summary", summary)]
    if rep["balances"]:
        b = rep["balances"]
        label_ = f"Synced accounts ({b['accounts']})" + (f", since {b['since'][5:]}" if b["since"] else "")
        out.append(("Balances", [(label_, f"{money(b['start'])} to {money(b['end'])}", money(b["delta"], sign=True))]))
    if rep["prev_label"]:
        out.append(("Spending by category", [("", "Spent", rep["prev_label"], "Change")] + [
            (c["category"], money(c["spent"]), money(c["prev"]), money(c["spent"] - c["prev"], sign=True))
            for c in rep["categories"]]))
    else:
        out.append(("Spending by category", [(c["category"], money(c["spent"])) for c in rep["categories"]]))
    out.append(("Top merchants", [(m["merchant"], money(m["spent"]), f"{m['n']}x") for m in rep["top_merchants"]]))
    out.append(("Largest charges", [(c["date"][5:], c["what"], money(c["amount"])) for c in rep["largest"]]))
    if rep["rental"]:
        r = rep["rental"]
        rows = [("Income", money(r["income"])), ("Expenses", money(r["expenses"])), ("Net", money(r["net"], sign=True))]
        if r["net_ex_principal"] is not None:
            rows.append(("Net excluding principal", money(r["net_ex_principal"], sign=True)))
        out.append(("Rental", rows))
    if rep["unusual"]:
        out.append(("Worth a look", [(u["date"][5:], u["label"], money(u["amount"]), u["reason"])
                                     for u in rep["unusual"]]))
    if rep["problems"]:
        out.append(("Sync problems", [(p,) for p in rep["problems"]]))
    return out


def subject(rep: dict) -> str:
    return f"Wealth Ledger: {rep['label']}: spent {money(rep['spent'])}, net {money(rep['net'], sign=True)}"


def _note(rep: dict) -> str:
    return "Complete transaction data starts partway through this month." if rep["partial"] else ""


def _is_num(c: str) -> bool:
    return c[:1] in ("$", "+", "-") and c[-1:].isdigit()


def render_text(rep: dict) -> str:
    out = [f"{rep['label']}", ""]
    if _note(rep):
        out += [_note(rep), ""]
    for title, rows in sections(rep):
        out.append(title.upper())
        widths = [max(len(r[i]) for r in rows if i < len(r)) for i in range(max(map(len, rows)))]
        for r in rows:
            cells = [c.rjust(widths[i]) if _is_num(c) or (i and not r[0]) else c.ljust(widths[i])
                     for i, c in enumerate(r)]
            out.append(("  " + "  ".join(cells)).rstrip())
        out.append("")
    return "\n".join(out)


def render_html(rep: dict) -> str:
    td = "padding:3px 14px 3px 0;border-bottom:1px solid #eee"
    out = [f'<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;color:#222;'
           f'max-width:640px"><h2 style="margin:0 0 4px">{escape(rep["label"])}</h2>']
    if _note(rep):
        out.append(f'<p style="color:#888;margin:0">{escape(_note(rep))}</p>')
    for title, rows in sections(rep):
        out.append(f'<h3 style="margin:18px 0 4px;font-size:15px">{escape(title)}</h3>'
                   '<table style="border-collapse:collapse;font-size:14px">')
        for r in rows:
            cells = "".join(
                f'<td style="{td};{"text-align:right;font-variant-numeric:tabular-nums;" if i and (_is_num(c) or not r[0]) else ""}'
                f'{"color:#b3261e;" if i and c.startswith("+") and title == "Spending by category" else ""}'
                f'">{escape(c)}</td>' for i, c in enumerate(r))
            out.append(f"<tr>{cells}</tr>")
        out.append("</table>")
    out.append("</div>")
    return "".join(out)


# ---------- sending ----------
def _gws(*args: str) -> dict:
    out = subprocess.run(["gws", *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"gws failed: {(out.stderr or out.stdout).strip()[:200]}")
    return json.loads(out.stdout)


def message(rep: dict, to: str) -> EmailMessage:
    msg = EmailMessage()
    msg["To"], msg["From"], msg["Subject"] = to, to, subject(rep)
    msg.set_content(render_text(rep))
    msg.add_alternative(render_html(rep), subtype="html")
    return msg


def send(rep: dict, to: str | None = None, gws=_gws) -> str:
    """Send to `to` (default: the Gmail account gws is signed in to). Returns the Gmail message id."""
    to = to or gws("gmail", "users", "getProfile", "--params", json.dumps({"userId": "me"}))["emailAddress"]
    raw = base64.urlsafe_b64encode(message(rep, to).as_bytes()).decode()
    return gws("gmail", "users", "messages", "send", "--params", json.dumps({"userId": "me"}),
               "--json", json.dumps({"raw": raw}))["id"]


def due(today: date, cfg: dict, state: Path = STATE) -> str | None:
    """The month to send now, or None: enabled, on or after the send day, and that month not sent yet."""
    if not cfg.get("enabled") or today.day < int(cfg.get("day") or SEND_DAY):
        return None
    month = last_month(today)
    return None if state.exists() and state.read_text().strip() >= month else month


def sync_problems(status: Path = STATUS) -> list[str]:
    try:
        return [l for l in status.read_text().splitlines()[1:] if l.strip()]
    except OSError:
        return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--month", help="YYYY-MM (default: last month)")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--if-due", action="store_true", help="send last month's email once, when enabled and due")
    ap.add_argument("--html", help="write the HTML body to this file")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args(argv)
    cfg, today = personal.section("monthly_email"), date.today()
    month = args.month or last_month(today)
    if args.if_due:
        month = due(today, cfg)
        if not month:
            print("monthly email: not due")
            return 0
    try:
        con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
        rep = report(con, month, today=today, since=personal.section("dashboard").get("ledger_start"),
                     rental_cfg=personal.section("rental"), categories=cfg.get("categories"),
                     problems=sync_problems())
        if args.html:
            Path(args.html).write_text(render_html(rep))
        if not (args.send or args.if_due):
            print(f"Subject: {subject(rep)}\n\n{render_text(rep)}")
            return 0
        mid = send(rep, cfg.get("to"))
        if month == last_month(today):
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(month + "\n")
        print(f"monthly email: sent {month} ({mid})")
        return 0
    except Exception as e:
        print(f"error: monthly email: {str(e)[:200]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
