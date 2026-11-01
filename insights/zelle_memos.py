#!/usr/bin/env python3
"""Zelle memos from the bank's Zelle emails: the bank feeds drop the note the sender typed ("rent", "deposit
returned"), but the email the bank sends for each Zelle has it. Reads Gmail with gws (read-only), parses two
email kinds and writes the note into txns.memo of the matching transaction:
  sent      "...Payment Details ... Amount $X ... To NAME (contact) Message NOTE Thank you..."
  received  "NAME sent you $X "NOTE" ... Delivery On or About MM/DD/YYYY"
A transaction matches when it is a Zelle (description mentions Zelle), has no memo yet, has the same amount and
direction, is dated within a few days of the email, and its description contains the other person's last name.
Only an unambiguous match is written. Feeds never overwrite a memo with an empty one (ctdb.upsert_sql), so a note
written here stays.

  python -m insights.zelle_memos                 last 30 days of email
  python -m insights.zelle_memos --days 400      backfill
  python -m insights.zelle_memos --dry-run       show matches, write nothing
Never sends notifications (owner's rule). The Gmail search is personal.json "zelle_memos": {"query": "..."}
(default: any email mentioning Zelle).
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctdb  # noqa: E402
import personal  # noqa: E402

DEFAULT_DB = Path(os.environ.get("WEALTH_LEDGER_DB") or "~/.local/share/wealth-ledger/txns.db").expanduser()
DEFAULT_QUERY = "zelle"
WINDOW = (-2, 5)  # posting date minus email date, in days
_AMT = r"\$([\d,]+\.\d\d)"
SENT = re.compile(r"Payment Details .*?Amount " + _AMT + r" .*? To (.+?) \([^)]*\) Message (.+?) Thank you", re.S)
RECEIVED = re.compile(r"([A-Z][A-Za-z.'-]*(?: [A-Za-z.'-]+)+) sent you " + _AMT + r' "([^"]*)"')
DELIVERY = re.compile(r"Delivery On or About (\d\d/\d\d/\d{4})")


def text_of(payload: dict) -> str:
    """Visible text of a Gmail message payload (its HTML parts, tags stripped, whitespace collapsed)."""
    out = []

    def walk(p):
        data = (p.get("body") or {}).get("data")
        if data and p.get("mimeType") in ("text/html", "text/plain"):
            out.append(base64.urlsafe_b64decode(data + "==").decode(errors="ignore"))
        for q in p.get("parts") or []:
            walk(q)
    walk(payload)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", " ".join(out)))).strip()


def parse(text: str, sent_on: date) -> dict | None:
    """One Zelle email -> {direction, amount, name, memo, date} (date = expected posting date), or None."""
    if m := SENT.search(text):
        amt, name, memo = m.groups()
        return {"direction": "out", "amount": float(amt.replace(",", "")), "name": name.strip(),
                "memo": memo.strip(), "date": sent_on}
    if m := RECEIVED.search(text):
        name, amt, memo = m.groups()
        d = DELIVERY.search(text)
        when = datetime.strptime(d.group(1), "%m/%d/%Y").date() if d else sent_on
        return {"direction": "in", "amount": float(amt.replace(",", "")), "name": name.strip(),
                "memo": memo.strip(), "date": when}
    return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").upper()


def match(con, note: dict, taken: set[str]) -> str | None:
    """txn_id of the one transaction this note belongs to, or None (no match, or more than one)."""
    last = _norm(note["name"]).split()[-1]
    lo, hi = (note["date"] + timedelta(WINDOW[0])).isoformat(), (note["date"] + timedelta(WINDOW[1])).isoformat()
    amt = -note["amount"] if note["direction"] == "out" else note["amount"]
    rows = [r for r in con.execute(
        "SELECT txn_id, description, date FROM v_ledger WHERE UPPER(description) LIKE '%ZELLE%' "
        "AND COALESCE(memo,'') = '' AND ABS(amt - ?) < 0.005 AND date BETWEEN ? AND ?", (amt, lo, hi))
        if r[0] not in taken]
    named = [r for r in rows if re.search(rf"\b{re.escape(last)}\b", _norm(r[1]))]
    # the bank shows the recipient's legal name, the email the contact name you saved ("Pat Cleaning"), so a
    # name miss still matches when it is the only Zelle of that amount and direction in the window
    pick = named if named else rows
    return pick[0][0] if len(pick) == 1 else None


def _gws(*args: str) -> dict:
    out = subprocess.run(["gws", "gmail", "users", "messages", *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"gws failed: {(out.stderr or out.stdout).strip()[:200]}")
    return json.loads(out.stdout)


def fetch_notes(days: int) -> list[dict]:
    query = personal.section("zelle_memos").get("query") or DEFAULT_QUERY
    params = {"userId": "me", "q": f"{query} newer_than:{days}d", "maxResults": 100}
    notes, page = [], None
    while True:
        res = _gws("list", "--params", json.dumps({**params, **({"pageToken": page} if page else {})}))
        for m in res.get("messages") or []:
            msg = _gws("get", "--params", json.dumps({"userId": "me", "id": m["id"]}))
            hdr = {h["name"]: h["value"] for h in msg["payload"].get("headers") or []}
            try:
                sent_on = parsedate_to_datetime(hdr.get("Date", "")).date()
            except (TypeError, ValueError):
                continue
            if note := parse(text_of(msg["payload"]), sent_on):
                notes.append(note)
        page = res.get("nextPageToken")
        if not page:
            return notes


def apply(con, notes: list[dict], dry_run: bool = False) -> list[tuple[str, dict]]:
    """Write each note to its matching transaction; returns [(txn_id, note)] for the ones matched."""
    done, taken = [], set()
    for note in sorted(notes, key=lambda n: n["date"]):
        if not note["memo"] or not (tid := match(con, note, taken)):
            continue
        taken.add(tid)
        done.append((tid, note))
        if not dry_run:
            con.execute("UPDATE txns SET memo = ? WHERE txn_id = ? AND COALESCE(memo,'') = ''", (note["memo"], tid))
    if not dry_run:
        con.commit()
    return done


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        notes = fetch_notes(args.days)
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"error: Zelle memos: {e}", file=sys.stderr)
        return 1
    con = ctdb.connect(args.db)
    done = apply(con, notes, args.dry_run)
    for tid, n in done:
        print(f"{n['date']} {'-' if n['direction'] == 'out' else '+'}{n['amount']:,.2f} {n['name']}: {n['memo']}")
    print(f"zelle memos: {len(notes)} emails with a note, {len(done)} {'would be ' if args.dry_run else ''}added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
