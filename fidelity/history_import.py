#!/usr/bin/env python3
"""Fidelity activity history (saved web responses) -> the wealth-ledger SQLite DB, for the years before the live feed.

  history_import.py [DIR] [--db PATH] [--dry-run] [--overlap-days 92] [--institution NAME]

DIR holds the JSON answers of fidelity.com's activity page, one file per date window
({"data": {"transactions": [...]}}), plus accounts.json ([{"acctNum", "name", "type"}], Fidelity's own names).
Rows are written to txns with source 'fidelity_web'.

No double counting: each Fidelity account is matched to the ledger account the live feed already has (by the
last 4 digits in its name "... (1234)", else by an unambiguous name) and shares its account_id; only rows dated
before that account's first live transaction are imported. The cutoff is read from the DB at every run, and
'fidelity_web' rows on or after it are removed, so a re-run stays correct once a feed starts covering an account.
Accounts the ledger has never seen get an id made from a hash of the account number, and an account_kind; when
a feed adds such an account later, a re-run moves its history under the feed's id.

The overlap (rows on or after the cutoff, for --overlap-days) is compared with the live feed per account and
mismatches are printed; nothing from it is imported. It also proves the sign: amounts are amtDetail.net as is
(negative = money out, like SimpleFIN), and the import refuses to run if the overlap says the sign is flipped.

Descriptions match the live feed's, so the same txn_rules classify them: Fidelity's `description` with account
numbers masked ("XXX-XXX123-1"); workplace-plan rows use the category word ("fees", "contribution"), with
Fidelity's text in the memo. Re-running is idempotent (the txn_id is a hash of the row). --dry-run opens the DB
read-only and prints the same report. Account numbers are never printed (last 4 only). Positions are not imported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctdb  # noqa: E402  shared schema

SOURCE = "fidelity_web"
DEFAULT_DIR = Path("~/.local/share/wealth-ledger/imports/fidelity-history").expanduser()
DEFAULT_DB = Path(os.environ.get("WEALTH_LEDGER_DB") or "~/.local/share/wealth-ledger/txns.db").expanduser()
INSTITUTION = "Fidelity Investments"  # the live feed's name for Fidelity
ACCOUNTS_FILE = "accounts.json"
OVERLAP_DAYS = 92
ET = ZoneInfo("America/New_York")  # Fidelity dates are midnight Eastern
ACCT_RE = re.compile(r"\b[A-Z0-9]\d{2}-\d{3}(\d{3}(?:-\d)?)\b")  # an account number inside a description
LAST4_RE = re.compile(r"\s*\((\d{4})\)\s*$")
MINOR_RE = re.compile(r"YOUTH|MINOR|UTMA|UGMA|CUSTODIAL", re.I)
CENT = Decimal("0.01")
KIND_NOTE = f"{SOURCE} history import"


class HistoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Row:
    acct_num: str
    date: str            # posted, ISO, Eastern
    transacted_at: str | None  # trade date, ISO datetime UTC
    amount: Decimal      # amtDetail.net: negative = money out
    description: str
    memo: str
    txn_id: str


@dataclass
class Account:
    acct_num: str
    label: str           # "<name> (<last 4>)", safe to print
    account_id: str
    matched: str         # 'last 4' | 'name' | 'new id'
    kind: str | None = None        # account_kind to add (new accounts only; a live account keeps its own)
    cutoff: str | None = None      # first live-feed date; None = the ledger has no transactions for it
    rows: list[Row] = field(default_factory=list)      # to import (before the cutoff)
    held_back: int = 0             # on or after the cutoff
    overlap: dict | None = None


# ---------- reading the export ----------
def _day(ts) -> str:
    return datetime.fromtimestamp(int(ts), ET).date().isoformat()


def _amount(v) -> Decimal:
    try:
        d = Decimal(str(v)).quantize(CENT)
    except (InvalidOperation, ValueError) as e:
        raise HistoryError(f"bad amount {v!r}") from e
    return d if d else Decimal("0.00")  # no "-0.00"


def describe(t: dict) -> tuple[str, str]:
    """(description, memo) the way the live feed words them."""
    desc = ACCT_RE.sub(r"XXX-XXX\1", (t.get("description") or "").strip())
    cat = t.get("catDetail") or {}
    if not cat.get("txnTypeDesc") and cat.get("txnCatDesc"):  # workplace plan: the feed shows the category word
        return cat["txnCatDesc"], desc
    return desc, ""


def _key(t: dict) -> tuple:
    dd = t.get("dateDetail") or {}
    return (str(t["acctNum"]), _day(dd.get("postedDate") or t["date"]), t.get("description") or "",
            str(_amount((t.get("amtDetail") or {}).get("net", 0))), str(t.get("quantity") or ""))


def load(folder: Path) -> tuple[list[dict], list[Row]]:
    """(accounts.json, every distinct transaction). A row repeated across overlapping windows counts once; exact
    repeats inside one window are separate transactions and each gets its own txn_id."""
    files = sorted(p for p in folder.glob("*.json") if p.name != ACCOUNTS_FILE)
    if not files:
        raise HistoryError(f"no .json files in {folder}")
    try:
        accounts = json.loads((folder / ACCOUNTS_FILE).read_text()) if (folder / ACCOUNTS_FILE).exists() else []
        count, sample = Counter(), {}
        for p in files:
            data = json.loads(p.read_text())
            if data.get("errors"):
                print(f"warn: {p.name} reports {len(data['errors'])} error(s) from Fidelity", file=sys.stderr)
            txns = (data.get("data") or {}).get("transactions")
            if txns is None:
                raise HistoryError(f"{p.name}: no data.transactions")
            per_file = Counter()
            for t in txns:
                k = _key(t)
                per_file[k] += 1
                sample.setdefault(k, t)
            for k, n in per_file.items():
                count[k] = max(count[k], n)
    except (ValueError, KeyError, TypeError) as e:
        raise HistoryError(f"unreadable export in {folder}: {type(e).__name__}: {e}") from None
    rows = []
    for k in sorted(count):
        t = sample[k]
        traded = (t.get("dateDetail") or {}).get("tradedDate")
        desc, memo = describe(t)
        traded = datetime.fromtimestamp(int(traded), timezone.utc).isoformat() if traded else None
        for n in range(count[k]):  # n tells exact repeats apart
            digest = hashlib.sha256("|".join((*k, str(n))).encode()).hexdigest()[:24]
            rows.append(Row(k[0], k[1], traded, Decimal(k[3]), desc, memo, f"{SOURCE}:{digest}"))
    return accounts, rows


# ---------- matching accounts ----------
def new_account_id(acct_num: str) -> str:
    """The id of an account the ledger has never seen: stable, and it does not show the account number."""
    return f"{SOURCE}:{hashlib.sha256(acct_num.encode()).hexdigest()[:16]}"


def guess_kind(name: str, ftype: str | None) -> str:
    if (ftype or "").lower() == "crypto":
        return "crypto"
    if MINOR_RE.search(name):
        return "custodial"
    for rx, kind in ctdb.KIND_GUESSES:
        if re.search(rx, name, re.I):
            return kind
    return "retirement" if ftype == "WPS" else "investment"


def ledger_accounts(db: sqlite3.Connection, institution: str) -> dict[str, str]:
    """account_id -> name of the institution's accounts another feed already has (superseded ones aside)."""
    return dict(db.execute(
        """SELECT account_id, account_name FROM (
             SELECT account_id, account_name, institution, 1 AS o FROM balances
             UNION ALL SELECT account_id, account_name, institution, 2 FROM txns WHERE source <> ?
             ORDER BY o)
           WHERE institution = ? AND account_id NOT IN (SELECT account_id FROM superseded)""", (SOURCE, institution)))


def map_accounts(acct_nums: list[str], accounts: list[dict], ledger: dict[str, str]) -> dict[str, Account]:
    """Fidelity account number -> ledger account: by the last 4 digits in the ledger name, else by a name only one
    account on each side has, else a new id hashed from the account number."""
    info = {str(a["acctNum"]): a for a in accounts}
    names = {n: (info.get(n, {}).get("name") or "Fidelity account").strip() for n in acct_nums}
    by_last4: dict[str, list[str]] = {}
    for aid, name in ledger.items():
        m = LAST4_RE.search(name or "")
        if m:
            by_last4.setdefault(m.group(1), []).append(aid)
    out: dict[str, Account] = {}
    for n in acct_nums:
        ids = by_last4.get(n[-4:], [])
        if len(ids) > 1:  # two ledger accounts end in the same digits: the name has to settle it
            ids = [i for i in ids if LAST4_RE.sub("", ledger[i]).casefold() == names[n].casefold()]
        if len(ids) == 1 and sum(x[-4:] == n[-4:] for x in acct_nums) == 1:
            out[n] = Account(n, ledger[ids[0]], ids[0], "last 4")
    free = {i: LAST4_RE.sub("", nm or "").casefold() for i, nm in ledger.items()
            if i not in {a.account_id for a in out.values()}}
    for n in acct_nums:
        if n in out:
            continue
        me = names[n].casefold()
        ids = [i for i, nm in free.items() if nm.startswith(me)]
        rivals = [x for x in acct_nums if x not in out and any(free[i].startswith(names[x].casefold()) for i in ids)]
        if len(ids) == 1 and rivals == [n]:
            out[n] = Account(n, ledger[ids[0]], ids[0], "name")
    for n in acct_nums:
        if n not in out:
            out[n] = Account(n, f"{names[n]} ({n[-4:]})", new_account_id(n), "new id")
        out[n].kind = guess_kind(names[n], info.get(n, {}).get("type"))
    return out


# ---------- plan ----------
def _overlap(db, acct: Account, mine: list[Row], end: str) -> dict:
    """This account's rows from the cutoff up to `end`, against the live feed's: count, sum, rows without a twin."""
    theirs = [(d, _amount(a), desc or "") for d, a, desc in db.execute(
        """SELECT COALESCE(posted, date(transacted_at)) d, amount, description FROM txns
           WHERE account_id = ? AND source <> ? AND pending = 0 AND d >= ? AND d < ?
             AND txn_id NOT IN (SELECT txn_id FROM late_duplicates)""", (acct.account_id, SOURCE, acct.cutoff, end))]
    mine_c = Counter((r.date, r.amount) for r in mine)
    theirs_c = Counter((d, a) for d, a, _ in theirs)
    lone = Counter({k: n for k, n in mine_c.items() if k[1] and (k[0], -k[1]) not in mine_c})  # no mirror row
    flipped = Counter({(d, -a): n for (d, a), n in lone.items()})
    only = []
    for side, extra, src in (("fidelity", mine_c - theirs_c, [(r.date, r.amount, r.description) for r in mine]),
                             ("ledger", theirs_c - mine_c, theirs)):
        for d, a, desc in sorted(src):
            if extra[(d, a)] > 0:
                extra[(d, a)] -= 1
                only.append((side, d, a, desc))
    return {"start": acct.cutoff, "end": end, "n": len(mine), "sum": sum((r.amount for r in mine), Decimal(0)),
            "ledger_n": len(theirs), "ledger_sum": sum((a for _, a, _ in theirs), Decimal(0)), "only": only,
            "same": sum((lone & theirs_c).values()), "flipped": sum((flipped & theirs_c).values())}


def plan(db: sqlite3.Connection, accounts: list[dict], rows: list[Row], institution: str = INSTITUTION,
         overlap_days: int = OVERLAP_DAYS) -> list[Account]:
    acct_nums = sorted({r.acct_num for r in rows})
    mapped = map_accounts(acct_nums, accounts, ledger_accounts(db, institution))
    first = dict(db.execute("SELECT account_id, MIN(COALESCE(posted, date(transacted_at))) FROM txns "
                            "WHERE source <> ? GROUP BY account_id", (SOURCE,)))
    has_kind = {r[0] for r in db.execute("SELECT account_id FROM account_kind")}
    export_end = (date.fromisoformat(max(r.date for r in rows)) + timedelta(days=1)).isoformat()
    for acct in mapped.values():
        mine = [r for r in rows if r.acct_num == acct.acct_num]
        acct.cutoff = first.get(acct.account_id)
        acct.kind = acct.kind if acct.matched == "new id" and acct.account_id not in has_kind else None
        acct.rows = [r for r in mine if not acct.cutoff or r.date < acct.cutoff]
        acct.held_back = len(mine) - len(acct.rows)
        if acct.cutoff:
            end = min((date.fromisoformat(acct.cutoff) + timedelta(days=overlap_days)).isoformat(), export_end)
            acct.overlap = _overlap(db, acct, [r for r in mine if acct.cutoff <= r.date < end], end)
    return sorted(mapped.values(), key=lambda a: (-len(a.rows) - a.held_back, a.label))


def sign_check(accts: list[Account]) -> tuple[int, int]:
    """Overlap rows with no same-day mirror image: (matching the live feed as is, matching with the sign flipped)."""
    return (sum(a.overlap["same"] for a in accts if a.overlap), sum(a.overlap["flipped"] for a in accts if a.overlap))


def report(accts: list[Account], institution: str) -> None:
    for a in accts:
        span = f"{min(r.date for r in a.rows)}..{max(r.date for r in a.rows)}" if a.rows else "-"
        cut = a.cutoff or "none (no live transactions)"
        print(f"{a.label}: import {len(a.rows)} rows {span}; cutoff {cut}; held back {a.held_back}; "
              f"account by {a.matched}" + (f", kind {a.kind}" if a.kind else ""))
        o = a.overlap
        if o:
            ok = not o["only"] and o["sum"] == o["ledger_sum"]
            print(f"  overlap {o['start']}..{o['end']}: fidelity {o['n']} rows sum {o['sum']} | ledger "
                  f"{o['ledger_n']} rows sum {o['ledger_sum']} | " + ("ok" if ok else f"MISMATCH ({len(o['only'])} rows)"))
            for side, d, amt, desc in o["only"]:
                print(f"    {side + ' only:':15}{d} {amt:>12} {desc[:70]}")
    same, flipped = sign_check(accts)
    print(f"sign check against {institution}: {same} one-sided overlap rows match as is, {flipped} only with "
          f"the sign flipped")


# ---------- storage ----------
def apply(db: sqlite3.Connection, accts: list[Account], institution: str) -> tuple[int, int, int]:
    """Write the plan. Returns (inserted, already there, removed because a live feed now covers them)."""
    now = datetime.now(timezone.utc).isoformat()
    removed = 0
    with db:
        for a in accts:
            if a.kind and a.rows:
                db.execute("INSERT OR IGNORE INTO account_kind VALUES (?,?,?)",
                           (a.account_id, a.kind, KIND_NOTE))
            if a.account_id != new_account_id(a.acct_num):  # the feed has this account now: drop its old hashed id
                removed += db.execute("DELETE FROM txns WHERE source = ? AND account_id = ?",
                                      (SOURCE, new_account_id(a.acct_num))).rowcount
                db.execute("DELETE FROM account_kind WHERE account_id = ? AND note = ?",
                           (new_account_id(a.acct_num), KIND_NOTE))
            if a.cutoff:
                removed += db.execute("DELETE FROM txns WHERE source = ? AND account_id = ? "
                                      "AND COALESCE(posted, date(transacted_at)) >= ?",
                                      (SOURCE, a.account_id, a.cutoff)).rowcount
        before = db.execute("SELECT COUNT(*) FROM txns").fetchone()[0]
        db.executemany(ctdb.upsert_sql("txns"), [
            (r.txn_id, a.account_id, a.label, institution, r.date, r.transacted_at, str(r.amount), r.description,
             r.description, r.memo, 0, "USD", now, now, SOURCE) for a in accts for r in a.rows])
        inserted = db.execute("SELECT COUNT(*) FROM txns").fetchone()[0] - before
    return inserted, sum(len(a.rows) for a in accts) - inserted, removed


def run(args) -> None:
    accounts, rows = load(args.dir)
    if not rows:
        raise HistoryError(f"no transactions in {args.dir}")
    if args.dry_run and args.db.exists():
        db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    elif args.dry_run:
        db = ctdb.connect(":memory:")
    else:
        args.db.parent.mkdir(parents=True, exist_ok=True)
        db = ctdb.connect(args.db)
    try:
        accts = plan(db, accounts, rows, args.institution, args.overlap_days)
        report(accts, args.institution)
        same, flipped = sign_check(accts)
        if flipped > same:
            raise HistoryError("the overlap matches the live feed only with the sign flipped; nothing imported")
        total = sum(len(a.rows) for a in accts)
        if args.dry_run:
            print(f"dry run: would import {total} of {len(rows)} rows, nothing written")
            return
        inserted, existing, removed = apply(db, accts, args.institution)
        print(f"rows={len(rows)} imported={total} inserted={inserted} already_there={existing} "
              f"removed_past_cutoff={removed} db={args.db}")
    finally:
        db.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", nargs="?", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true", help="print the plan and the overlap check, write nothing")
    ap.add_argument("--overlap-days", type=int, default=OVERLAP_DAYS)
    ap.add_argument("--institution", default=INSTITUTION, help="the ledger's institution name for Fidelity")
    args = ap.parse_args(argv)
    try:
        run(args)
    except HistoryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
