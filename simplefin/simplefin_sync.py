#!/usr/bin/env python3
"""SimpleFIN Bridge -> SQLite transaction sync (requests + stdlib only).

  simplefin_sync.py claim <setup-token>       # one-time; saves Access URL (0600)
  simplefin_sync.py sync [--days 30] [--start YYYY-MM-DD --end YYYY-MM-DD]

sync re-pulls the last --days days. When an account comes back after a longer gap (its connection was stuck, the
sync didn't run) or is new, it also fetches the days in between, up to SimpleFIN's 90-day history, in the same run.

The Access URL embeds basic-auth credentials: treat it as a password. It is
never printed in full; override its location with --access-file or the
SIMPLEFIN_ACCESS_URL env var.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import os
import sys
import time
from dataclasses import dataclass, astuple
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctdb  # noqa: E402  shared schema

DEFAULT_ACCESS_FILE = Path(os.environ.get("WEALTH_LEDGER_ACCESS") or Path(
    os.environ.get("WEALTH_LEDGER_CONFIG_DIR") or "~/.config/wealth-ledger") / "simplefin_access_url").expanduser()
DEFAULT_DB = Path(os.environ.get("WEALTH_LEDGER_DB") or "~/.local/share/wealth-ledger/txns.db").expanduser()
MAX_WINDOW_DAYS = 45  # hard limit 90d; server warns (gen.api) above 45d and may cap later
TIMEOUT = (10, 60)
RETRIES = 4


class SimpleFinError(RuntimeError):
    pass


@dataclass(frozen=True)
class Txn:
    txn_id: str          # "<account_id>:<id>" (ids are only unique per account)
    account_id: str
    account_name: str
    institution: str
    posted: str | None   # ISO date, None while pending (posted == 0)
    transacted_at: str | None  # ISO datetime UTC
    amount: str          # Decimal as string (exact)
    description: str
    payee: str
    memo: str
    pending: bool
    currency: str = "USD"


# ---------- helpers ----------
def mask(url: str) -> str:
    p = urlsplit(url)
    host = p.hostname or ""
    return f"{p.scheme}://***:***@{host}{p.path}"


def _ts(v) -> datetime | None:
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(v, timezone.utc) if v > 0 else None


def _dec(v) -> Decimal:
    try:
        return Decimal(str(v))
    except InvalidOperation as e:
        raise SimpleFinError(f"bad amount {v!r}") from e


def _request(method: str, url: str, **kw) -> requests.Response:
    """HTTP with timeout + exponential backoff on 429/5xx/network errors."""
    delay = 2.0
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.request(method, url, timeout=TIMEOUT, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            if attempt == RETRIES:
                raise SimpleFinError(f"{method} failed after {RETRIES} tries: {type(e).__name__}") from None
            print(f"warn: {type(e).__name__}; retry {attempt}/{RETRIES - 1} in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


# ---------- claim ----------
def claim(token: str, access_file: Path) -> None:
    try:
        claim_url = base64.b64decode(token.strip(), validate=True).decode()
    except (binascii.Error, UnicodeDecodeError):
        raise SimpleFinError("setup token is not valid base64") from None
    if not claim_url.startswith("https://"):
        raise SimpleFinError("decoded claim URL is not https; refusing")
    r = _request("POST", claim_url)
    if r.status_code == 403:
        raise SimpleFinError("claim rejected (403): token invalid or already used. Generate a new one.")
    r.raise_for_status()
    access_url = r.text.strip()
    if "@" not in access_url:
        raise SimpleFinError("unexpected claim response")
    access_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(access_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(access_url + "\n")
    os.chmod(access_file, 0o600)
    print(f"Claimed. Access URL saved to {access_file} (0600): {mask(access_url)}")


def load_access_url(access_file: Path) -> str:
    env = os.environ.get("SIMPLEFIN_ACCESS_URL")
    if env:
        return env.strip()
    if not access_file.exists():
        raise SimpleFinError(f"no access URL at {access_file}; run `claim` first")
    if access_file.stat().st_mode & 0o077:
        raise SimpleFinError(f"{access_file} is group/world accessible; chmod 600 it")
    return access_file.read_text().strip()


# ---------- fetch ----------
def fetch_window(access_url: str, start: datetime, end: datetime, pending: bool = True) -> dict:
    p = urlsplit(access_url)
    auth = (p.username or "", p.password or "")
    netloc = p.hostname + (f":{p.port}" if p.port else "")
    base = urlunsplit((p.scheme, netloc, p.path.rstrip("/") + "/accounts", "", ""))
    params = {"version": "2", "start-date": int(start.timestamp()), "end-date": int(end.timestamp())}
    if pending:
        params["pending"] = "1"
    r = _request("GET", base, params=params, auth=auth)
    if r.status_code == 402:
        raise SimpleFinError("402 payment required: SimpleFIN subscription lapsed")
    if r.status_code == 403:
        raise SimpleFinError("403: access token invalid or revoked; re-claim a new setup token")
    r.raise_for_status()
    return r.json()


def windows(start: date, end: date, size: int = MAX_WINDOW_DAYS):
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=size), end)
        yield (datetime.combine(cur, datetime.min.time(), timezone.utc),
               datetime.combine(nxt, datetime.min.time(), timezone.utc))
        cur = nxt


def warnings_from(data: dict) -> list[str]:
    out = []
    for e in data.get("errlist") or []:
        out.append(f"{e.get('code', '?')}: {e.get('msg', '')} (conn={e.get('conn_id')}, acct={e.get('account_id')})")
    for e in data.get("errors") or []:  # deprecated v1 field
        out.append(f"legacy: {e}")
    return out


STALE_DAYS = 4  # a connection whose newest balance is older than this is stuck (weekends included)


def stale_warnings(data: dict, now: datetime, max_days: int = STALE_DAYS) -> list[str]:
    """One warning per connection whose accounts all have an old balance-date. SimpleFIN keeps reporting such a
    connection as OK with no errlist entry (Coinbase stuck 10/16..11/01), so this is the only sign of it.
    Inside a connection that is otherwise fresh, each account that holds money and has an old balance-date gets
    its own warning: the fresh siblings would hide it."""
    conns = {c.get("conn_id"): c.get("name") for c in data.get("connections") or []}
    newest: dict[str, datetime] = {}
    for a in data.get("accounts") or []:
        bd = _ts(a.get("balance-date"))
        c = a.get("conn_id")
        if bd and (c not in newest or bd > newest[c]):
            newest[c] = bd
    out = []
    for c, bd in sorted(newest.items(), key=lambda kv: kv[1]):
        age = (now - bd).days
        if age > max_days:
            out.append(f"stale: Connection to {conns.get(c) or c} has not updated since {bd.date()} ({age} days); "
                       f"open beta-bridge.simplefin.org > My account > Adjust on it to refresh (conn={c})")
    for a in data.get("accounts") or []:
        bd, c = _ts(a.get("balance-date")), a.get("conn_id")
        if not bd or (now - bd).days <= max_days or (now - newest[c]).days > max_days:
            continue  # fresh, or its whole connection is already named above
        try:
            empty = not Decimal(str(a.get("balance") or 0))
        except InvalidOperation:
            empty = False
        if not empty:
            out.append(f"stale: Account {a.get('name') or a.get('id')} at {conns.get(c) or c} has not updated since "
                       f"{bd.date()} ({(now - bd).days} days) while the rest of its connection has; its balance is "
                       f"from then (conn={c}, acct={a.get('id')})")
    return out


BACKFILL_MAX_DAYS = 90     # SimpleFIN keeps about 90 days of history
BACKFILL_OVERLAP_DAYS = 7  # re-read this many days before the gap, for rows that were pending when it began


def known_accounts(db_path: Path) -> dict[str, str | None]:
    """account_id -> newest balance-date (ISO, None if the feed never gave one) stored by earlier syncs."""
    if not db_path.exists():
        return {}
    db = ctdb.connect(db_path)
    try:
        return dict(db.execute("SELECT account_id, MAX(as_of) FROM balances WHERE source = 'simplefin' "
                               "GROUP BY account_id"))
    finally:
        db.close()


def backfill_from(data: dict, known: dict[str, str | None], skip: set[str], start: date, end: date) -> date | None:
    """The day to fetch back to when the usual window [start, end) leaves a hole, else None.

    A fixed window only works while every account is read at least that often. An account whose balance-date just
    moved forward after standing still since before `start` (connection stuck or broken, or no sync ran) has
    transactions between its old balance-date and `start` that no later run would ever ask for. An account never
    seen before has its whole history missing. Either way go back to that point (less an overlap), at most
    BACKFILL_MAX_DAYS. It happens once: the new balance-date is stored by this run. Accounts in `skip` (fed by
    another source now) never count."""
    floor = end - timedelta(days=BACKFILL_MAX_DAYS)
    need = []
    for a in data.get("accounts") or []:
        bd = _ts(a.get("balance-date"))
        if a["id"] in skip or not bd:
            continue
        if a["id"] not in known:
            need.append(floor)
            continue
        try:
            prev = datetime.fromisoformat(known[a["id"]]).date()
        except (TypeError, ValueError):
            continue
        if prev < start and bd.date() > prev:
            need.append(prev - timedelta(days=BACKFILL_OVERLAP_DAYS))
    back = max(floor, min(need)) if need else None
    return back if back and back < start else None


def normalize(data: dict) -> list[Txn]:
    conns = {c.get("conn_id"): c.get("name") for c in data.get("connections") or []}
    rows = []
    for a in data.get("accounts") or []:
        inst = (conns.get(a.get("conn_id")) or (a.get("org") or {}).get("name")
                or (a.get("org") or {}).get("domain") or "")
        for t in a.get("transactions") or []:
            posted = _ts(t.get("posted"))
            tat = _ts(t.get("transacted_at"))
            desc = t.get("description") or ""
            extra = t.get("extra") or {}
            rows.append(Txn(
                txn_id=f"{a['id']}:{t['id']}", account_id=a["id"], account_name=a.get("name", ""),
                institution=inst,
                posted=posted.date().isoformat() if posted else None,
                transacted_at=tat.isoformat() if tat else None,
                amount=str(_dec(t.get("amount", "0"))), description=desc,
                payee=str(extra.get("payee") or t.get("payee") or desc),
                memo=str(extra.get("memo") or t.get("memo") or ""),
                pending=bool(t.get("pending", False)),
                currency=a.get("currency", "USD") if len(a.get("currency", "USD")) == 3 else "USD",
            ))
    return rows


# ---------- storage ----------
def superseded_ids(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    db = ctdb.connect(db_path)
    try:
        return {r[0] for r in db.execute("SELECT account_id FROM superseded WHERE source = 'simplefin'")}
    finally:
        db.close()


def superseded_institutions(db_path: Path) -> set[str]:
    """SimpleFIN institutions whose every known account was moved to another feed."""
    if not db_path.exists():
        return set()
    db = ctdb.connect(db_path)
    try:
        return {r[0] for r in db.execute(
            "SELECT institution FROM balances WHERE source = 'simplefin' AND institution <> '' GROUP BY institution "
            "HAVING SUM(account_id NOT IN (SELECT account_id FROM superseded)) = 0")}
    finally:
        db.close()


def upsert(db_path: Path, rows: list[Txn], holdings: list[tuple], balances: list[tuple]) -> tuple[int, int]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    db = ctdb.connect(db_path)
    with db:
        # accounts now fed by another source (see ctdb.superseded) are skipped entirely
        gone = superseded_ids(db_path)
        rows = [r for r in rows if r.account_id not in gone]
        holdings = [h for h in holdings if h[2] not in gone]
        balances = [b for b in balances if b[2] not in gone]
        before = db.execute("SELECT COUNT(*) FROM txns").fetchone()[0]
        db.executemany(ctdb.upsert_sql("txns"),
                       [(*astuple(r)[:11], r.currency, now, now, "simplefin") for r in rows])
        db.executemany(ctdb.UPSERT_HOLDING, holdings)
        db.executemany(ctdb.UPSERT_BALANCE, balances)
        after = db.execute("SELECT COUNT(*) FROM txns").fetchone()[0]
    db.close()
    return after - before, len(rows) - (after - before)


def balances_from(data: dict) -> list[tuple]:
    conns = {c.get("conn_id"): c.get("name") for c in data.get("connections") or []}
    today = date.today().isoformat()
    out = []
    for a in data.get("accounts") or []:
        bd = _ts(a.get("balance-date"))
        avail = a.get("available-balance")
        out.append((today, "simplefin", a["id"], a.get("name", ""), conns.get(a.get("conn_id"), ""),
                    str(_dec(a.get("balance") or 0)), None if avail in (None, "") else str(_dec(avail)),
                    a.get("currency", "USD") if len(a.get("currency", "USD")) == 3 else "USD",
                    bd.isoformat() if bd else None))
    return out


def holdings_from(data: dict) -> list[tuple]:
    """Today's position snapshot (SimpleFIN reports cost_basis as 0, so it is stored as NULL)."""
    conns = {c.get("conn_id"): c.get("name") for c in data.get("connections") or []}
    today = date.today().isoformat()
    out = []
    for a in data.get("accounts") or []:
        for h in a.get("holdings") or []:
            shares, value = _dec(h.get("shares") or 0), _dec(h.get("market_value") or 0)
            cb = _dec(h.get("cost_basis") or 0)
            out.append((today, "simplefin", a["id"], a.get("name", ""), conns.get(a.get("conn_id"), ""),
                        h.get("id") or h.get("symbol") or h.get("description", ""), h.get("symbol") or "",
                        h.get("description") or "", str(shares),
                        str((value / shares).quantize(Decimal("0.0001"))) if shares else None,
                        str(value), str(cb) if cb else None, h.get("currency") or "USD"))
    return out


def sync(args) -> None:
    access_url = load_access_url(args.access_file)
    end = date.fromisoformat(args.end) if args.end else date.today() + timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days)
    all_rows: dict[str, Txn] = {}
    warns: set[str] = set()
    holdings: list[tuple] = []
    balances: list[tuple] = []
    conn_accts: dict[str, set[str]] = {}
    gone = superseded_ids(args.db)
    known = known_accounts(args.db)
    for ws, we in windows(start, end):
        data = fetch_window(access_url, ws, we)
        warns.update(warnings_from(data))
        warns.update(stale_warnings(data, datetime.now(timezone.utc)))
        for a in data.get("accounts") or []:
            conn_accts.setdefault(a.get("conn_id"), set()).add(a["id"])
        holdings, balances = holdings_from(data), balances_from(data)  # point-in-time: keep the last window's
        for r in normalize(data):
            all_rows[r.txn_id] = r
        print(f"window {ws.date()}..{we.date()}: {len(data.get('accounts') or [])} accounts", file=sys.stderr)
    # `data` is the newest window: does it show an account that came back after a gap this window doesn't cover?
    back = None if args.start else backfill_from(data, known, gone, start, end)
    if back:
        print(f"backfill: an account is new or came back after a gap; also fetching {back}..{start}", file=sys.stderr)
        for ws, we in windows(back, start):
            older = fetch_window(access_url, ws, we)
            warns.update(warnings_from(older))
            for r in normalize(older):
                all_rows.setdefault(r.txn_id, r)  # the newest window's copy of a row wins
            print(f"window {ws.date()}..{we.date()}: {len(older.get('accounts') or [])} accounts", file=sys.stderr)
    # connections whose every account moved to another feed (ctdb.superseded) can't hurt us: no phone alert
    # (a broken connection may return no accounts, so also match the institution name in the message)
    muted = {f"conn={c}" for c, ids in conn_accts.items() if c and ids <= gone}
    muted |= {f"acct={a})" for a in gone}
    muted |= {f"Connection to {i} " for i in superseded_institutions(args.db)}
    for w in sorted(warns):
        quiet = any(m in w for m in muted)
        print(f"{'note (superseded connection)' if quiet else 'WARNING'}: {w}", file=sys.stderr)
    new, updated = upsert(args.db, list(all_rows.values()), holdings, balances)
    db = ctdb.connect(args.db)
    for name, kind in ctdb.apply_account_kinds(db):  # personal.json "account_kinds"
        print(f"account type from personal.json: {name} -> {kind}", file=sys.stderr)
    if ctdb.personal.auto_kind():  # nobody classifies this ledger's accounts by hand
        guessed, unknown = ctdb.guess_kinds(db)
        for name, kind in guessed:
            print(f"account type guessed from its name: {name} -> {kind}", file=sys.stderr)
        for name in unknown:
            print(f"WARNING: account \"{name}\" has no type yet and is left out of the totals; name its type in "
                  f"personal.json \"account_kinds\" (see README)", file=sys.stderr)
    for name, day, lag in ctdb.lagging_holdings(db, "simplefin"):
        print(f"WARNING: holdings: {name} positions were last reported {day} ({lag} days behind the feed); shown as "
              f"of that day", file=sys.stderr)
    db.close()
    print(f"fetched={len(all_rows)} inserted={new} updated={updated} holdings={len(holdings)} "
          f"balances={len(balances)} db={args.db}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--access-file", type=Path, default=DEFAULT_ACCESS_FILE)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim"); c.add_argument("token")
    s = sub.add_parser("sync")
    s.add_argument("--db", type=Path, default=DEFAULT_DB)
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--start"); s.add_argument("--end")
    args = ap.parse_args(argv)
    try:
        claim(args.token, args.access_file) if args.cmd == "claim" else sync(args)
    except SimpleFinError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
