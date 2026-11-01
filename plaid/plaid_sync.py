#!/usr/bin/env python3
"""Plaid (Trial plan) -> the wealth-ledger SQLite DB, for accounts SimpleFIN can't reach.

  plaid_sync.py setup <client_id> <secret> [--env production|sandbox]   save keys (0600)
  plaid_sync.py link [--products investments] [--compare]
                                           open a local Plaid Link page, store the item's access token.
                                           --compare: the account is already fed by SimpleFIN, so its
                                           transactions go to txns_compare, never the main ledger.
  plaid_sync.py relink [institution]       update mode: re-authenticate an existing item (keeps its history)
  plaid_sync.py sandbox-link               sandbox only: create a test item without the Link UI
  plaid_sync.py sync [--db PATH]           /transactions/sync every linked item into txns.db

Amounts are stored SimpleFIN-style (negative = spend); Plaid's own sign is the opposite.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctdb  # noqa: E402  shared schema
import personal  # noqa: E402

CONFIG = Path(os.environ.get("PLAID_CONFIG", "~/.config/wealth-ledger/plaid.json")).expanduser()
DEFAULT_DB = Path("~/.local/share/wealth-ledger/txns.db").expanduser()
HOSTS = {"production": "https://production.plaid.com", "sandbox": "https://sandbox.plaid.com"}
LINK_PORT = 8899
TIMEOUT = (10, 60)

class PlaidError(RuntimeError):
    pass


def client_user_id() -> str:
    """Stable per-user id Plaid Link wants (personal.json plaid.client_user_id)."""
    return personal.section("plaid").get("client_user_id") or "wealth-ledger-user"


# ---------- config ----------
def load_config() -> dict:
    if not CONFIG.exists():
        raise PlaidError(f"no {CONFIG}; run `setup <client_id> <secret>` first")
    if CONFIG.stat().st_mode & 0o077:
        raise PlaidError(f"{CONFIG} is group/world accessible; chmod 600 it")
    return json.loads(CONFIG.read_text())


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG)


def call(cfg: dict, path: str, body: dict, retries: int = 3) -> dict:
    url = HOSTS[cfg["env"]] + path
    payload = {"client_id": cfg["client_id"], "secret": cfg["secret"], **body}
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            if attempt == retries:
                raise PlaidError(f"{path}: {e}") from None
        else:
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.ok:
                return data
            code = data.get("error_code", r.status_code)
            if r.status_code < 500 and code != "RATE_LIMIT_EXCEEDED":
                raise PlaidError(f"{path}: {code}: {data.get('error_message', r.text[:200])}")
        time.sleep(2 ** attempt)
    raise PlaidError(f"{path}: gave up after {retries + 1} attempts")


# ---------- linking ----------
def store_item(cfg: dict, public_token: str, institution: str,
               products=("transactions",), mode: str = "primary") -> dict:
    ex = call(cfg, "/item/public_token/exchange", {"public_token": public_token})
    item = {"item_id": ex["item_id"], "access_token": ex["access_token"], "institution": institution,
            "products": list(products), "mode": mode, "cursor": None}
    cfg["items"] = [i for i in cfg.get("items", []) if i["item_id"] != item["item_id"]] + [item]
    save_config(cfg)
    return item


LINK_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Link a card</title>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script></head>
<body style="font:16px system-ui;margin:3em"><h2>wealth-ledger: link an account</h2><p id="s">Opening Plaid…</p>
<script>
const s = document.getElementById('s');
const h = Plaid.create({token: "%s",
  onSuccess: (pt, md) => fetch('/done', {method:'POST', body: JSON.stringify({public_token: pt,
      institution: (md.institution||{}).name || ''})}).then(r => r.text()).then(t => s.textContent = t),
  onEvent: (ev, md) => { if (ev === 'ERROR') fetch('/event', {method:'POST', body: JSON.stringify(md)}); },
  onExit: (err, md) => { s.textContent = err ? 'Error: ' + (err.display_message || err.error_message) : 'Cancelled.';
      fetch('/done', {method:'POST', body: JSON.stringify({exit: {error: err, ...md}})}); }});
h.open();
</script></body></html>"""


def link(cfg: dict, update_item: dict | None = None, products=("transactions",), mode="primary") -> None:
    body = {"user": {"client_user_id": client_user_id()}, "client_name": "wealth-ledger",
            "country_codes": ["US"], "language": "en"}
    if update_item:  # update mode: same item and transaction ids, nothing to exchange afterwards
        body["access_token"] = update_item["access_token"]
    else:
        body["products"] = list(products)
        if "transactions" in products:
            body["transactions"] = {"days_requested": 730}
    resp = call(cfg, "/link/token/create", body)
    lt = resp["link_token"]
    print(f"link token request_id {resp.get('request_id')}")
    done = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body: str, ctype="text/plain"):
            self.send_response(200); self.send_header("Content-Type", ctype); self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            self._send(LINK_HTML % lt, "text/html")

        def do_POST(self):
            msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            if self.path == "/event" or "exit" in msg:  # ids Plaid support asks for on a failed link
                md = msg.get("exit", msg)
                err = md.get("error") or {}
                print("link", "error" if self.path == "/event" else "exit", {k: md.get(k) for k in (
                    "link_session_id", "request_id", "status", "error_code", "error_type")}
                      | {k: err[k] for k in ("error_code", "error_type", "error_message") if k in err})
                if self.path == "/event":
                    return self._send("ok")
            if msg.get("public_token") and update_item:
                text = f"Re-authenticated {update_item.get('institution') or update_item['item_id']}."
                print(text)
            elif msg.get("public_token"):
                item = store_item(cfg, msg["public_token"], msg.get("institution", ""), products, mode)
                text = f"Linked {item['institution'] or item['item_id']}. You can close this tab."
                print(text)
            else:
                text = "Nothing linked."
            self._send(text); done.set()

    srv = http.server.HTTPServer(("127.0.0.1", LINK_PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://localhost:{LINK_PORT}/"
    print(f"Open {url} to link (waiting up to 30 min)…")
    webbrowser.open(url)
    done.wait(1800)
    srv.shutdown()


# ---------- sync ----------
def account_names(cfg: dict, item: dict, db: sqlite3.Connection | None = None) -> dict[str, str]:
    """Account display names; for primary items also snapshot balances (free /accounts/get, cached data).
    Compare items are skipped: SimpleFIN already owns those balances."""
    accts = call(cfg, "/accounts/get", {"access_token": item["access_token"]})["accounts"]
    names = {a["account_id"]: f"{a.get('official_name') or a['name']} ({a.get('mask') or '?'})" for a in accts}
    if db is not None and item.get("mode", "primary") == "primary":
        today = datetime.now().date().isoformat()
        rows = []
        for a in accts:
            b = a["balances"]
            owed = a["type"] in ("credit", "loan")  # Plaid reports amounts owed as positive
            cur = None if b.get("current") is None else (-b["current"] if owed else b["current"])
            rows.append((today, "plaid", a["account_id"], names[a["account_id"]], item.get("institution") or "",
                         None if cur is None else f"{cur:.2f}",
                         None if b.get("available") is None else f"{b['available']:.2f}",
                         b.get("iso_currency_code") or "USD", b.get("last_updated_datetime")))
        db.executemany(ctdb.UPSERT_BALANCE, rows)
    return names


def to_row(t: dict, names: dict, institution: str, now: str) -> tuple:  # matches ctdb.upsert_sql
    amount = -t["amount"]  # Plaid: positive = money out
    return (t["transaction_id"], t["account_id"], names.get(t["account_id"], t["account_id"]), institution,
            None if t["pending"] else t["date"], t.get("datetime") or t.get("authorized_datetime"),
            f"{amount:.2f}", t["name"], t.get("merchant_name") or "", t.get("pending_transaction_id") or "",
            int(t["pending"]), t.get("iso_currency_code") or "USD", now, now, "plaid")


def sync_item(cfg: dict, item: dict, db: sqlite3.Connection) -> tuple[int, int, int]:
    names = account_names(cfg, item, db)
    now = datetime.now(timezone.utc).isoformat()
    start_cursor = item.get("cursor")
    while True:  # restart the whole pagination if Plaid reports a mutation mid-way
        cursor, added, modified, removed = start_cursor, [], [], []
        try:
            while True:
                body = {"access_token": item["access_token"], "count": 500}
                if cursor:
                    body["cursor"] = cursor
                page = call(cfg, "/transactions/sync", body)
                added += page["added"]; modified += page["modified"]; removed += page["removed"]
                cursor = page["next_cursor"]
                if not page["has_more"]:
                    break
            break
        except PlaidError as e:
            if "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" not in str(e):
                raise
    inst = item.get("institution") or ""
    table = "txns_compare" if item.get("mode") == "compare" else "txns"
    db.executemany(ctdb.upsert_sql(table), [to_row(t, names, inst, now) for t in added + modified])
    db.executemany(f"DELETE FROM {table} WHERE txn_id = ?", [(r["transaction_id"],) for r in removed])
    db.commit()
    item["cursor"] = cursor  # only advance after the DB write succeeded
    return len(added), len(modified), len(removed)


def sync_holdings(cfg: dict, item: dict, db: sqlite3.Connection) -> int:
    """Daily snapshot via /investments/holdings/get only (the Holdings subscription; never call
    /investments/transactions/get, which adds the separately billed Investments Transactions)."""
    r = call(cfg, "/investments/holdings/get", {"access_token": item["access_token"]})
    names = {a["account_id"]: f"{a.get('official_name') or a['name']} ({a.get('mask') or '?'})"
             for a in r["accounts"]}
    secs = {x["security_id"]: x for x in r["securities"]}
    today = datetime.now().date().isoformat()
    rows = []
    for h in r["holdings"]:
        sec = secs.get(h["security_id"], {})
        rows.append((today, "plaid", h["account_id"], names.get(h["account_id"], ""), item.get("institution") or "",
                     h["security_id"], sec.get("ticker_symbol") or "", sec.get("name") or "",
                     str(h["quantity"]), str(h.get("institution_price")), str(h.get("institution_value")),
                     None if h.get("cost_basis") is None else str(h["cost_basis"]),
                     h.get("iso_currency_code") or "USD"))
    hold_sql = ctdb.UPSERT_HOLDING
    if item.get("mode") == "compare":  # another feed owns these positions; keep Plaid's copy out of the views
        hold_sql = hold_sql.replace("INTO holdings ", "INTO holdings_compare ")
    db.executemany(hold_sql, rows)
    if item.get("mode", "primary") == "primary":  # Plaid owns this institution: keep its balances too
        db.executemany(ctdb.UPSERT_BALANCE, [
            (today, "plaid", a["account_id"], names[a["account_id"]], item.get("institution") or "",
             None if a["balances"].get("current") is None else f"{a['balances']['current']:.2f}",
             None if a["balances"].get("available") is None else f"{a['balances']['available']:.2f}",
             a["balances"].get("iso_currency_code") or "USD", a["balances"].get("last_updated_datetime"))
            for a in r["accounts"]])
    db.commit()
    return len(rows)


def sync(cfg: dict, db_path: Path) -> None:
    if not cfg.get("items"):
        raise PlaidError("no linked items; run `link` first")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    failed = 0
    db = ctdb.connect(db_path)
    try:
        for item in cfg["items"]:
            label = item.get("institution") or item["item_id"]
            products = item.get("products", ["transactions"])
            try:
                if "transactions" in products:
                    a, m, r = sync_item(cfg, item, db)
                    print(f"{label}: added={a} modified={m} removed={r} ({item.get('mode', 'primary')})")
                if "investments" in products:
                    print(f"{label}: holdings={sync_holdings(cfg, item, db)}")
            except PlaidError as e:
                failed += 1
                print(f"error: {label}: {e}", file=sys.stderr)
        for dup, orig in ctdb.flag_late_duplicates(db):
            print(f"late duplicate hidden: {dup} (copy of {orig})")
    finally:
        db.close()
    save_config(cfg)
    if failed:
        raise PlaidError(f"{failed} item(s) failed")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup"); s.add_argument("client_id"); s.add_argument("secret")
    s.add_argument("--env", choices=HOSTS, default="production")
    lk = sub.add_parser("link")
    lk.add_argument("--products", default="transactions", help="comma list, e.g. investments")
    lk.add_argument("--compare", action="store_true")
    sub.add_parser("sandbox-link")
    rl = sub.add_parser("relink"); rl.add_argument("institution", nargs="?", default="")
    y = sub.add_parser("sync"); y.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "setup":
            cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {"items": []}
            if cfg.get("env") not in (None, args.env):
                cfg["items"] = []  # items are environment-specific
            cfg.update(client_id=args.client_id, secret=args.secret, env=args.env)
            save_config(cfg)
            print(f"Saved {args.env} keys to {CONFIG} (0600)")
            return 0
        cfg = load_config()
        if args.cmd == "link":
            link(cfg, products=tuple(args.products.split(",")), mode="compare" if args.compare else "primary")
        elif args.cmd == "relink":
            matches = [i for i in cfg.get("items", []) if args.institution.lower() in (i.get("institution") or "").lower()]
            if len(matches) != 1:
                raise PlaidError(f"{len(matches)} items match {args.institution!r}; name one of: "
                                 + ", ".join(i.get("institution") or i["item_id"] for i in cfg.get("items", [])))
            link(cfg, matches[0])
        elif args.cmd == "sandbox-link":
            if cfg["env"] != "sandbox":
                raise PlaidError("sandbox-link needs --env sandbox keys")
            pt = call(cfg, "/sandbox/public_token/create",
                      {"institution_id": "ins_109508", "initial_products": ["transactions"]})["public_token"]
            print("Linked", store_item(cfg, pt, "First Platypus Bank (sandbox)")["item_id"])
        else:
            sync(cfg, args.db)
    except PlaidError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
