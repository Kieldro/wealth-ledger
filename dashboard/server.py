#!/usr/bin/env python3
"""Private finance dashboard for wealth-ledger. Read-only; serves on 127.0.0.1 + the Tailscale IP only.

  server.py [--port 8766]
  server.py set-password [--generate]     # set/rotate the login password (signs out all sessions)

Every path needs a login session (cookie) except /login, /logout (POST) and /robots.txt.
Endpoints: /api/summary, POST /api/refresh (re-runs networth.py), /api/trend, /api/accounts,
/api/holdings, /api/cards, /api/spending, /api/drafts (overdraft forecast), /api/subscriptions (recurring charges +
unusual charges), /healthz. Page: /
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import string
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape as html_escape
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import personal  # noqa: E402
from insights import overdraft, subscriptions  # noqa: E402

_DASH = personal.section("dashboard")
DB = os.path.expanduser("~/.local/share/wealth-ledger/txns.db")
STATUS = os.path.expanduser("~/.cache/wealth-ledger-status")
NETWORTH = [os.path.join(ROOT, ".venv", "bin", "python"), os.path.join(ROOT, "networth.py"), "--json"]
CACHE_TTL = 600
LEDGER_START = _DASH.get("ledger_start") or "1970-01-01"  # first day of complete transaction data
# accounts networth.py counts as live lines (mirrors its EXTRA_KINDS; crypto/custodial are excluded there)
NW_KINDS = ("credit", "retirement", "checking", "savings", "investment", "hsa", "loan")

# ---------- categories ----------
CATEGORIES = [  # key, label, type
    ("cash", "Cash & banking", "asset"),
    ("retirement", "Retirement", "asset"),
    ("taxable", "Taxable brokerage", "asset"),
    ("crypto", "Crypto", "asset"),
    ("metals", "Precious metals", "asset"),
    ("realestate", "Real estate", "asset"),
    ("vehicles", "Vehicles", "asset"),
    ("other", "Other", "asset"),
    ("mortgage", "Mortgage", "liability"),
    ("cards", "Credit cards & other debt", "liability"),
]
# networth.py line label -> category, then (regex on line label, category) fallbacks; both from personal.json
# "dashboard" because the labels are the owner's own balance-sheet rows. Lines matching neither go to "other".
DEFAULT_RULES = [
    (r"\b(ira|roth|401\(?k|403b|hsa|pre-tax|pension)\b", "retirement"),
    (r"checking|savings|bank|cash|wallet", "cash"),
    (r"crypto|bitcoin", "crypto"),
    (r"metal|gold|silver", "metals"),
    (r"home|house|property", "realestate"),
    (r"car\b|truck|vehicle", "vehicles"),
    (r"brokerage|individual|taxable", "taxable"),
]
EXACT: dict[str, str] = dict(_DASH.get("exact") or {})
RULES: list[tuple[str, str]] = [tuple(r) for r in _DASH["rules"]] if _DASH.get("rules") else DEFAULT_RULES


def categorize(line: dict) -> str:
    name = line["line"]
    if line["type"] == "liability":
        return "mortgage" if re.search(r"mortgage", name, re.I) else "cards"
    if name in EXACT:
        return EXACT[name]
    for pat, cat in RULES:
        if re.search(pat, name, re.I):
            return cat
    return "other"


# ---------- data access ----------
def db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def q(sql: str, args=()) -> list[dict]:
    con = db()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


_nw_lock = threading.Lock()
_nw = {"data": None, "fetched": 0.0, "error": None}


def networth(refresh: bool = False) -> dict:
    with _nw_lock:
        fresh = _nw["data"] is not None and time.time() - _nw["fetched"] < CACHE_TTL
        if refresh or not fresh:
            try:
                out = subprocess.run(NETWORTH, capture_output=True, text=True, timeout=60, check=True).stdout
                _nw.update(data=json.loads(out), fetched=time.time(), error=None)
            except Exception as e:  # keep serving the stale copy
                msg = getattr(e, "stderr", "") or str(e)
                _nw["error"] = f"networth.py failed: {str(msg).strip()[-300:]}"
        return dict(_nw)


def read_status() -> dict:
    try:
        lines = [l.rstrip() for l in open(STATUS) if l.strip()]
    except OSError as e:
        return {"last_run": None, "rc": None, "problems": [f"status file unreadable: {e.strerror}"]}
    m = re.match(r"last run (\S+) rc=(\d+)", lines[0]) if lines else None
    return {"last_run": m.group(1) if m else None, "rc": int(m.group(2)) if m else None,
            "problems": lines[1:] if m else lines}


def synced_series() -> list[dict]:
    """Daily sum of synced balances networth.py counts (non-custodial/crypto, non-superseded), carried forward."""
    rows = q(f"""SELECT b.snapshot_date d, b.account_id a, CAST(b.balance AS REAL) bal FROM balances b
                 JOIN account_kind k USING (account_id)
                 WHERE k.kind IN ({','.join('?' * len(NW_KINDS))})
                   AND b.account_id NOT IN (SELECT account_id FROM superseded)
                 ORDER BY b.snapshot_date""", NW_KINDS)
    dates = sorted({r["d"] for r in rows})
    last, out = {}, []
    by_date: dict[str, list] = {}
    for r in rows:
        by_date.setdefault(r["d"], []).append(r)
    for d in dates:
        for r in by_date[d]:
            last[r["a"]] = r["bal"] or 0.0
        out.append({"date": d, "synced": round(sum(last.values()), 2)})
    return out


# ---------- endpoints ----------
def api_summary(params) -> dict:
    nw = networth(refresh="refresh" in params)
    data = nw["data"]
    res = {"status": read_status(), "networth_error": nw["error"],
           "networth_fetched": datetime.fromtimestamp(nw["fetched"]).astimezone().isoformat() if nw["fetched"] else None,
           "cache_ttl_s": CACHE_TTL}
    if not data:
        return res
    cats = {k: {"key": k, "label": lbl, "type": t, "value": 0.0, "live": 0.0, "sheet": 0.0, "lines": []}
            for k, lbl, t in CATEGORIES}
    for l in data["lines"]:
        if abs(l["used"] or 0) < 0.005:
            continue
        c = cats[categorize(l)]
        signed = l["used"] if l["type"] == "asset" else -l["used"]
        live = l["from"].startswith("live")
        c["value"] += signed
        c["live" if live else "sheet"] += signed
        c["lines"].append({"line": l["line"], "value": round(signed, 2), "source": "live" if live else "sheet"})
    for c in cats.values():
        c["lines"].sort(key=lambda x: -abs(x["value"]))
        for k in ("value", "live", "sheet"):
            c[k] = round(c[k], 2)
    sheet_const = round(sum((l["used"] if l["type"] == "asset" else -l["used"])
                            for l in data["lines"] if not l["from"].startswith("live")), 2)
    # change vs N days ago, using the trend (synced history + today's sheet-only total held constant)
    series = synced_series()
    changes = {}
    if series:
        cur_synced = series[-1]["synced"]
        latest = date.fromisoformat(series[-1]["date"])
        for n in (1, 7, 30):
            target = (latest - timedelta(days=n)).isoformat()
            past = [s for s in series if s["date"] <= target]
            if past and past[-1]["date"] == target:
                changes[str(n)] = {"delta": round(cur_synced - past[-1]["synced"], 2), "vs": target}
            else:
                changes[str(n)] = {"delta": None, "vs": target, "note": f"history starts {series[0]['date']}"}
    res.update(net_worth=data["net_worth"], assets=data["assets"], liabilities=data["liabilities"],
               sheet_net_worth=data.get("sheet_net_worth"), sheet_only_total=sheet_const,
               changes=changes, categories=list(cats.values()))
    return res


def api_trend(params) -> dict:
    nw = networth()
    series = synced_series()
    sheet_const = None
    if nw["data"]:
        sheet_const = round(sum((l["used"] if l["type"] == "asset" else -l["used"])
                                for l in nw["data"]["lines"] if not l["from"].startswith("live")), 2)
    for s in series:
        s["total"] = round(s["synced"] + sheet_const, 2) if sheet_const is not None else None
    return {"series": series, "sheet_only_total": sheet_const,
            "note": "Synced accounts from daily snapshots (custodial accounts, crypto exchange wallets and "
                    "superseded feeds excluded, as in networth.py) plus today's sheet-only total held constant."}


def api_accounts(params) -> dict:
    rows = q("""SELECT account_name, institution, source, ROUND(balance,2) balance, as_of, snapshot_date,
                       COALESCE(kind,'unclassified') kind
                FROM v_accounts ORDER BY kind, ABS(balance) DESC""")
    for r in rows:
        r["as_of"] = (r["as_of"] or "")[:16].replace("T", " ") or r["snapshot_date"]
        r["counted"] = r["kind"] in NW_KINDS
    return {"accounts": rows}


def api_holdings(params) -> dict:
    rows = q("""SELECT h.symbol, h.description, h.account_name, h.value, h.shares, h.price, h.source, h.snapshot_date
                FROM v_holdings_latest h LEFT JOIN account_kind k USING (account_id)
                WHERE TRIM(COALESCE(h.symbol,'')) <> '' AND COALESCE(k.kind,'') <> 'custodial'""")
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["symbol"], {"symbol": r["symbol"], "description": r["description"], "value": 0.0,
                                         "accounts": [], "sources": set(), "as_of": r["snapshot_date"]})
        a["value"] += r["value"] or 0
        if abs(r["value"] or 0) >= 0.01:
            a["accounts"].append(re.sub(r"\s*\([0-9a-f-]+\)$", "", r["account_name"] or ""))
        a["sources"].add(r["source"])
        a["as_of"] = min(a["as_of"], r["snapshot_date"])
    total = sum(a["value"] for a in agg.values())
    out = sorted((a for a in agg.values() if abs(a["value"]) >= 0.01), key=lambda a: -a["value"])
    for a in out:
        a["value"] = round(a["value"], 2)
        a["pct"] = round(100 * a["value"] / total, 2) if total else 0
        a["accounts"] = sorted(set(a["accounts"]))
        a["sources"] = sorted(a["sources"])
    return {"holdings": out, "total": round(total, 2), "count": len(out),
            "note": "Excludes custodial accounts and blank-symbol cash sweeps. SimpleFIN has no cost basis, so no gains. "
                    "Manual holdings are as of their last manual entry."}


def api_cards(params) -> dict:
    rows = q("""SELECT account_name, institution, source, ROUND(-balance,2) owed, as_of, snapshot_date
                FROM v_accounts WHERE kind='credit' ORDER BY balance""")
    for r in rows:
        r["as_of"] = (r["as_of"] or "")[:16].replace("T", " ") or r["snapshot_date"]
    return {"cards": rows, "total": round(sum(r["owed"] for r in rows), 2)}


def api_drafts(params) -> dict:
    con = db()
    try:
        fc = overdraft.forecast(con)
    finally:
        con.close()
    return {"accounts": [a for a in fc if a["events"]], "warnings": overdraft.warnings(fc),
            "horizon_days": overdraft.HORIZON, "buffer": overdraft.BUFFER}


def api_subscriptions(params) -> dict:
    con = db()
    try:
        rep = subscriptions.report(con)
    finally:
        con.close()
    for r in rep["subscriptions"] + rep["unusual"]:
        r["merchant"] = merchant(r["description"], r["payee"])
        r["account"] = re.sub(r"\s*\([0-9a-f-]+\)$", "", r["account"] or "")
    return rep


GENERIC_PAYEES = {"investment buy", "investment sell", "payment", "transfer", "deposit", "withdrawal"}


def merchant(desc: str, payee: str | None) -> str:
    s = (payee or "").strip()
    if not s or s.lower() in GENERIC_PAYEES:
        s = desc or s
    s = re.sub(r"\s+\(?cash\)?$", "", s, flags=re.I).strip()
    s = re.sub(r"^(direct debit|debit card purchase|pos debit)\s+", "", s, flags=re.I)
    s = re.sub(r"\s*[#*].*$", "", s)                 # ONLINE MKTPL*XYZ, GROCERY #123 ...
    s = re.sub(r"\s+\d[\d\s-]*.*$", "", s) if not re.match(r"^\d", s) else s  # trailing store numbers
    return re.sub(r"\s+", " ", s).strip().title() or "(unknown)"


def api_spending(params) -> dict:
    monthly = q("""SELECT strftime('%Y-%m', date) month, ROUND(-SUM(amt),2) spent, COUNT(*) n FROM v_ledger
                   WHERE class='spend' AND pending=0 AND date >= ? GROUP BY month ORDER BY month""", (LEDGER_START,))
    today = date.today()
    this_m = today.strftime("%Y-%m")
    last_m = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    tops = {}
    for m in (this_m, last_m):
        rows = q("""SELECT description, payee, amt FROM v_ledger WHERE class='spend' AND pending=0
                    AND strftime('%Y-%m', date)=? AND date >= ?""", (m, LEDGER_START))
        agg: dict[str, list] = {}
        for r in rows:
            k = merchant(r["description"], r["payee"])
            a = agg.setdefault(k, [0.0, 0])
            a[0] -= r["amt"]
            a[1] += 1
        tops[m] = [{"merchant": k, "spent": round(v[0], 2), "n": v[1]}
                   for k, v in sorted(agg.items(), key=lambda kv: -kv[1][0])[:10]]
    recent = q("""SELECT date, account_name, ROUND(amt,2) amt, description, payee, class, pending FROM v_ledger
                  ORDER BY date DESC, first_seen DESC, txn_id LIMIT 30""")
    for r in recent:
        r["merchant"] = merchant(r["description"], r["payee"])
        r["account_name"] = re.sub(r"\s*\([0-9a-f-]+\)$", "", r["account_name"] or "")
    return {"monthly": monthly, "this_month": this_m, "last_month": last_m, "top": tops, "recent": recent,
            "since": LEDGER_START,
            "note": f"class='spend', posted only, since {LEDGER_START} (complete data starts then; that month is partial)."}


ROUTES = {"/api/summary": api_summary, "/api/trend": api_trend, "/api/accounts": api_accounts,
          "/api/holdings": api_holdings, "/api/cards": api_cards, "/api/spending": api_spending,
          "/api/drafts": api_drafts, "/api/subscriptions": api_subscriptions}


# ---------- auth ----------
# Every path except /login, /logout and /robots.txt needs a valid session, including loopback requests
# (cloudflared connects from 127.0.0.1). Credentials: AUTH_FILE (0600, scrypt hash + session-signing secret).
CONF_DIR = os.path.expanduser("~/.config/wealth-ledger")
AUTH_FILE = os.path.join(CONF_DIR, "dashboard_auth.json")
SESSIONS_FILE = os.path.join(CONF_DIR, "dashboard_sessions.json")
COOKIE = "ctd_session"
SESSION_TTL = 30 * 86400
SCRYPT = {"n": 2 ** 17, "r": 8, "p": 1}
SCRYPT_MAXMEM = 256 * 1024 * 1024
IP_MAX_FAILS, IP_WINDOW = 5, 15 * 60
GLOBAL_MAX_FAILS, GLOBAL_WINDOW, GLOBAL_LOCK = 50, 60 * 60, 15 * 60
MAX_SESSIONS = 100
COOKIE_RE = re.compile(r"v1\.[A-Za-z0-9_-]{43}\.\d{10}\.[A-Za-z0-9_-]{43}")


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _kdf(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32)


def _write_private(path: str, data: dict):
    """Atomically write JSON with mode 0600."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Auth:
    def __init__(self):
        self.lock = threading.Lock()        # credentials + session store
        self.login_lock = threading.Lock()  # serializes password checks so the limits can't be raced
        self._key = None
        self._creds = None
        self.sessions: dict[str, int] = {}  # sha256(session id) -> expiry
        self.ip_fails: dict[str, deque] = {}
        self.global_fails: deque = deque()
        self.global_locked_until = 0.0

    # credentials (reloaded when the file changes, so set-password needs no restart; fail closed)
    def creds(self) -> dict | None:
        with self.lock:
            try:
                st = os.stat(AUTH_FILE)
                key = (st.st_mtime_ns, st.st_ino, st.st_size, st.st_mode)
            except OSError as e:
                st, key = None, ("err", e.errno)
            if key == self._key:
                return self._creds
            self._key, self._creds = key, None
            if st is None:
                log(f"auth: {AUTH_FILE} unreadable ({os.strerror(key[1])}); all logins refused. "
                    f"Run: server.py set-password")
            elif st.st_mode & 0o077:
                log(f"auth: {AUTH_FILE} has mode {oct(st.st_mode & 0o777)}; must be 0600. All logins refused.")
            else:
                try:
                    with open(AUTH_FILE) as f:
                        c = json.load(f)
                    creds = {"username": str(c["username"]), "salt": _b64d(c["salt"]), "hash": _b64d(c["hash"]),
                             "n": int(c["n"]), "r": int(c["r"]), "p": int(c["p"]),
                             "secret": bytes.fromhex(c["session_secret"])}
                    if len(creds["secret"]) < 32 or len(creds["hash"]) != 32 or len(creds["salt"]) < 16:
                        raise ValueError("short secret/hash/salt")
                    self._creds = creds
                except Exception as e:
                    log(f"auth: {AUTH_FILE} invalid ({type(e).__name__}); all logins refused")
            self.sessions = self._load_sessions()
            return self._creds

    def _secret_id(self) -> str:
        return hashlib.sha256(b"sid" + self._creds["secret"]).hexdigest()[:16] if self._creds else ""

    def _load_sessions(self) -> dict:
        if not self._creds:
            return {}
        try:
            with open(SESSIONS_FILE) as f:
                d = json.load(f)
            if d.get("secret_id") != self._secret_id():  # password rotated: old sessions are void
                return {}
            now = time.time()
            return {k: int(v) for k, v in d["sessions"].items() if int(v) > now}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def _save_sessions(self):  # caller holds self.lock
        now = time.time()
        live = sorted(((k, v) for k, v in self.sessions.items() if v > now), key=lambda kv: kv[1])
        self.sessions = dict(live[-MAX_SESSIONS:])
        try:
            _write_private(SESSIONS_FILE, {"secret_id": self._secret_id(), "sessions": self.sessions})
        except OSError as e:
            log(f"auth: could not save sessions: {e}")

    # sessions: cookie = v1.<sid>.<exp>.<HMAC-SHA256(secret, "v1.<sid>.<exp>")>, sid must also be in the store
    @staticmethod
    def _sign(secret: bytes, msg: str) -> str:
        return _b64e(hmac.new(secret, msg.encode(), hashlib.sha256).digest())

    @staticmethod
    def _sid_key(sid: str) -> str:
        return hashlib.sha256(sid.encode()).hexdigest()

    def new_session(self) -> str:
        c = self.creds()
        if not c:
            raise RuntimeError("credentials vanished during login")
        sid, exp = _b64e(secrets.token_bytes(32)), int(time.time()) + SESSION_TTL
        msg = f"v1.{sid}.{exp}"
        with self.lock:
            self.sessions[self._sid_key(sid)] = exp
            self._save_sessions()
        return f"{msg}.{self._sign(c['secret'], msg)}"

    def check(self, value: str) -> str | None:
        """Session-store key if the cookie value is a valid, unexpired, unrevoked session; else None."""
        c = self.creds()
        if not c or not COOKIE_RE.fullmatch(value or ""):
            return None
        _, sid, exp, sig = value.split(".")
        if not hmac.compare_digest(self._sign(c["secret"], f"v1.{sid}.{exp}").encode(), sig.encode()):
            return None
        now = time.time()
        if int(exp) <= now:
            return None
        k = self._sid_key(sid)
        with self.lock:
            stored = self.sessions.get(k)
        return k if stored and stored > now else None

    def revoke(self, values: list[str]):
        keys = [k for k in (self.check(v) for v in values) if k]
        if keys:
            with self.lock:
                for k in keys:
                    self.sessions.pop(k, None)
                self._save_sessions()

    # login + brute-force limits (in memory; a service restart clears them)
    @staticmethod
    def _bucket(ip: str) -> str:
        """Rate-limit key: the address, or its /64 for IPv6 (one host usually owns the whole /64)."""
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return ip
        return str(ipaddress.ip_network(f"{a}/64", strict=False)) if a.version == 6 else str(a)

    def _retry_after(self, ip: str, now: float) -> int:
        if now < self.global_locked_until:
            return int(self.global_locked_until - now) + 1
        ip = self._bucket(ip)
        q = self.ip_fails.get(ip)
        if q:
            while q and q[0] <= now - IP_WINDOW:
                q.popleft()
            if len(q) >= IP_MAX_FAILS:
                return int(q[0] + IP_WINDOW - now) + 1
            if not q:
                del self.ip_fails[ip]
        return 0

    def _record_failure(self, ip: str, now: float):
        q = self.ip_fails.setdefault(self._bucket(ip), deque())
        q.append(now)
        g = self.global_fails
        g.append(now)
        while g and g[0] <= now - GLOBAL_WINDOW:
            g.popleft()
        msg = (f"auth: login FAILED ip={ip} ({len(q)}/{IP_MAX_FAILS} for this ip in 15 min, "
               f"{len(g)}/{GLOBAL_MAX_FAILS} total in 1 h)")
        if len(q) >= IP_MAX_FAILS:
            msg += "; ip locked out for 15 min"
        if len(g) >= GLOBAL_MAX_FAILS:
            self.global_locked_until = now + GLOBAL_LOCK
            g.clear()
            msg += "; GLOBAL cap hit, all logins locked for 15 min"
        log(msg)
        if len(self.ip_fails) > 10000:
            for k in [k for k, v in self.ip_fails.items() if not v or v[-1] <= now - IP_WINDOW]:
                del self.ip_fails[k]

    def login(self, ip: str, username: str, password: str) -> tuple[str, object]:
        """('ok', cookie) | ('bad', None) | ('locked', retry_after_s)"""
        if not self.login_lock.acquire(timeout=15):
            return "locked", 5
        try:
            now = time.time()
            retry = self._retry_after(ip, now)
            if retry:
                log(f"auth: login refused (locked out) ip={ip} retry_after={retry}s")
                return "locked", retry
            c = self.creds()
            if c:
                pw_ok = hmac.compare_digest(_kdf(password, c["salt"], c["n"], c["r"], c["p"]), c["hash"])
                user_ok = hmac.compare_digest(username.encode(), c["username"].encode())
            else:  # no usable credentials: same work, always fail
                _kdf(password, b"\0" * 16, **SCRYPT)
                pw_ok = user_ok = False
            if pw_ok and user_ok:
                self.ip_fails.pop(self._bucket(ip), None)
                log(f"auth: login ok ip={ip}")
                return "ok", self.new_session()
            self._record_failure(ip, now)
            return "bad", None
        finally:
            self.login_lock.release()


AUTH = Auth()


def gen_passphrase() -> str:
    """6 groups of 4 random lowercase letters (~113 bits), easy to type on a phone."""
    return "-".join("".join(secrets.choice(string.ascii_lowercase) for _ in range(4)) for _ in range(6))


def cli_set_password(argv: list[str]):
    ap = argparse.ArgumentParser(prog="server.py set-password",
                                 description="Set or rotate the dashboard password. Signs out every session; "
                                             "the running server picks it up on its next request.")
    ap.add_argument("--username", help="default: keep the current one, else personal.json dashboard.default_username")
    ap.add_argument("--generate", action="store_true", help="generate a random passphrase and print it once")
    a = ap.parse_args(argv)
    username = a.username
    if not username:
        try:
            with open(AUTH_FILE) as f:
                username = json.load(f)["username"]
        except (OSError, ValueError, KeyError):
            username = _DASH.get("default_username") or "admin"
    if a.generate:
        pw = gen_passphrase()
    else:
        pw = getpass.getpass("New password: ")
        if pw != getpass.getpass("Repeat: "):
            sys.exit("passwords don't match; nothing changed")
        if len(pw) < 12:
            sys.exit("use at least 12 characters; nothing changed")
    salt = secrets.token_bytes(16)
    _write_private(AUTH_FILE, {
        "username": username, "kdf": "scrypt", **SCRYPT, "salt": _b64e(salt), "hash": _b64e(_kdf(pw, salt, **SCRYPT)),
        "session_secret": secrets.token_bytes(32).hex(), "updated": datetime.now().astimezone().isoformat(timespec="seconds")})
    try:
        os.remove(SESSIONS_FILE)
    except FileNotFoundError:
        pass
    print(f"Password set for '{username}' in {AUTH_FILE}; all sessions signed out.")
    if a.generate:
        print(f"New password (shown once): {pw}")


# ---------- http ----------
ROBOTS = b"User-agent: *\nDisallow: /\n"
CHARTJS = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"
CSP_BASE = "default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
CSP_LOGIN = f"{CSP_BASE}; style-src 'unsafe-inline'; img-src data:"
LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta name="referrer" content="no-referrer">
<title>Sign in · Finance Dashboard</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='4' fill='%232a78d6'/%3E%3Cpath d='M3 11l3-3 2 2 5-5' stroke='white' stroke-width='1.8' fill='none'/%3E%3C/svg%3E">
<style>
:root { color-scheme: light; --bg: #f4f4f2; --surface: #fcfcfb; --border: #e3e2de; --text: #0b0b0b; --text-2: #52514e;
  --muted: #7a7974; --accent: #2a78d6; --neg: #c0362c; --err-bg: #fbe9e7; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --bg: #111110; --surface: #1a1a19; --border: #2e2e2c;
  --text: #f0efec; --text-2: #c3c2b7; --muted: #8f8e88; --accent: #3987e5; --neg: #f07068; --err-bg: #3a1a17; } }
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--bg); color: var(--text); min-height: 100vh; display: flex; align-items: center; justify-content: center;
  font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, "Helvetica Neue", Arial, sans-serif;
  -webkit-text-size-adjust: 100%; padding: 16px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 22px 20px; width: 100%; max-width: 360px; }
h1 { font-size: 15px; font-weight: 600; margin: 0 0 2px; color: var(--text-2); letter-spacing: .02em; }
.sub { font-size: 12px; color: var(--muted); margin: 0 0 18px; }
label { display: block; font-size: 12px; color: var(--text-2); margin: 0 0 4px; }
input { width: 100%; font: inherit; font-size: 16px; color: var(--text); background: var(--bg); border: 1px solid var(--border);
  border-radius: 10px; padding: 10px 12px; margin: 0 0 14px; }
input:focus { outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent; }
button { width: 100%; font: inherit; font-size: 15px; font-weight: 600; color: #fff; background: var(--accent); border: 0;
  border-radius: 10px; padding: 11px; cursor: pointer; margin-top: 4px; }
.err { background: var(--err-bg); color: var(--neg); border-radius: 10px; padding: 8px 12px; margin: 0 0 14px; font-size: 13px; }
</style>
</head>
<body>
<main class="card">
  <h1>Finances</h1>
  <p class="sub">Private dashboard. Sign in to continue.</p>
  {{error}}
  <form method="post" action="/login" autocomplete="on">
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" required autofocus>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
</main>
</body>
</html>
"""


def index_csp(page: bytes) -> str:
    hashes = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(m).digest()).decode()
                      for m in re.findall(rb"<script>(.*?)</script>", page, re.S))
    return (f"{CSP_BASE}; script-src {hashes} {CHARTJS}; style-src 'self' 'unsafe-inline'; "
            f"img-src 'self' data:; connect-src 'self'")


class Handler(BaseHTTPRequestHandler):
    server_version = "wealth-ledger-dashboard"
    sys_version = ""

    def version_string(self):
        return self.server_version
    timeout = 30  # drop slow/idle clients

    def log_message(self, fmt, *args):  # quiet; errors and auth events go to the journal via log()
        pass

    # --- helpers
    def _https(self) -> bool:
        h = getattr(self, "headers", None)
        if h is None:
            return False
        return (h.get("X-Forwarded-Proto", "").strip().lower() == "https"
                or "https" in h.get("Cf-Visitor", "").lower())

    def end_headers(self):  # security headers on every response, including stdlib error pages
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        self.send_header("Content-Security-Policy", getattr(self, "_csp", None) or CSP_BASE)
        if self._https():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def _send(self, code: int, body: bytes, ctype: str, headers=(), csp: str | None = None):
        self._csp = csp
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj, headers=()):
        return self._send(code, json.dumps(obj).encode(), "application/json", headers)

    def _redirect(self, to: str, headers=()):
        return self._send(303, b"", "text/plain", [("Location", to), *headers])

    def _client_ip(self) -> str:
        peer = self.client_address[0]
        cf = self.headers.get("Cf-Connecting-IP", "").strip()
        # trust the tunnel's header only from loopback (where cloudflared connects), never from the tailnet
        if cf and peer in ("127.0.0.1", "::1") and len(cf) <= 45 and re.fullmatch(r"[0-9A-Fa-f:.]+", cf):
            return cf
        return peer

    def _cookie_values(self) -> list[str]:
        out = []
        for h in self.headers.get_all("Cookie") or []:
            for part in h.split(";"):
                name, _, val = part.strip().partition("=")
                if name == COOKIE:
                    out.append(val.strip().strip('"'))
        return out[:5]

    def _authed(self) -> bool:
        return any(AUTH.check(v) for v in self._cookie_values())

    def _set_cookie(self, value: str, max_age: int) -> tuple[str, str]:
        c = f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict"
        return ("Set-Cookie", c + "; Secure" if self._https() else c)

    def _deny(self, path: str):
        html = path in ("/", "/index.html") or (
            not path.startswith("/api/") and path != "/healthz" and "text/html" in self.headers.get("Accept", ""))
        return self._redirect("/login") if html else self._json(401, {"error": "unauthorized"})

    def _cross_site(self) -> bool:
        # SameSite=Strict already keeps the cookie off cross-site requests; also refuse cross-site POSTs outright
        return self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none")

    def _read_form(self, limit: int = 4096) -> dict | None:
        n = self.headers.get("Content-Length", "0").strip()
        if not (n.isascii() and n.isdigit()) or int(n) > limit:
            return None
        body = self.rfile.read(int(n)).decode("utf-8", "replace") if int(n) else ""
        try:
            return parse_qs(body, max_num_fields=10)
        except ValueError:
            return None

    def _login_page(self, code: int = 200, error: str = "", headers=()):
        err = f'<p class="err" role="alert">{html_escape(error)}</p>' if error else ""
        return self._send(code, LOGIN_HTML.replace("{{error}}", err).encode(), "text/html; charset=utf-8", headers, CSP_LOGIN)

    # --- routes
    def do_GET(self):
        p = "?"
        try:
            u = urlparse(self.path)
            p = u.path
            if p == "/robots.txt":
                return self._send(200, ROBOTS, "text/plain; charset=utf-8")
            if p == "/login":
                return self._redirect("/") if self._authed() else self._login_page()
            if p == "/logout":
                return self._send(405, b"use POST\n", "text/plain", [("Allow", "POST")])
            if not self._authed():
                return self._deny(p)
            if p in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    page = f.read()
                return self._send(200, page, "text/html; charset=utf-8", csp=index_csp(page))
            if p == "/healthz":
                return self._send(200, b"ok\n", "text/plain")
            fn = ROUTES.get(p)
            if not fn:
                return self._json(404, {"error": "not found"})
            params = parse_qs(u.query)
            params.pop("refresh", None)  # refresh is POST /api/refresh only
            return self._json(200, fn(params))
        except Exception as e:
            log(f"error on GET {p}: {e!r}")
            return self._json(500, {"error": str(e) if self._authed() else "internal error"})

    def do_POST(self):
        p = "?"
        try:
            p = urlparse(self.path).path
            if self._cross_site():
                log(f"auth: refused cross-site POST {p} ip={self._client_ip()}")
                return self._json(403, {"error": "cross-site request refused"})
            form = self._read_form()
            if form is None:
                return self._json(413, {"error": "bad or oversized request body"})
            if p == "/login":
                user = form.get("username", [""])[0][:256]
                pw = form.get("password", [""])[0][:1024]
                status, extra = AUTH.login(self._client_ip(), user, pw)
                if status == "ok":
                    return self._redirect("/", [self._set_cookie(extra, SESSION_TTL)])
                if status == "locked":
                    mins = max(1, round(extra / 60))
                    return self._login_page(429, f"Too many failed attempts. Try again in about {mins} min.",
                                            [("Retry-After", str(extra))])
                return self._login_page(401, "Wrong username or password.")
            if p == "/logout":
                AUTH.revoke(self._cookie_values())
                return self._redirect("/login", [self._set_cookie("", 0)])
            if not self._authed():
                return self._deny(p)
            if p == "/api/refresh":  # re-reads the balance sheet via networth.py; POST so it can't be triggered cross-site
                return self._json(200, api_summary({"refresh": ["1"]}))
            return self._json(404, {"error": "not found"})
        except Exception as e:
            log(f"error on POST {p}: {e!r}")
            return self._json(500, {"error": "internal error"})


def tailscale_ip() -> str | None:
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10).stdout.split()
        return out[0] if out else None
    except Exception:
        return None


def serve(host: str, port: int):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"listening on http://{host}:{port}/", flush=True)
    httpd.serve_forever()


def tailnet_binder(port: int):
    """Bind the Tailscale IP once tailscaled has one; retry every 30 s so boot order doesn't matter."""
    while True:
        ip = tailscale_ip()
        if ip:
            try:
                serve(ip, port)
            except OSError as e:
                print(f"bind {ip}:{port} failed: {e}; retrying", file=sys.stderr, flush=True)
        time.sleep(30)


def main():
    if sys.argv[1:2] == ["set-password"]:
        return cli_set_password(sys.argv[2:])
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", 8766)))
    ap.add_argument("--no-tailscale", action="store_true")
    a = ap.parse_args()
    if not a.no_tailscale:
        threading.Thread(target=tailnet_binder, args=(a.port,), daemon=True).start()
    if AUTH.creds():
        log(f"auth: credentials loaded from {AUTH_FILE}")
    threading.Thread(target=networth, daemon=True).start()  # warm the cache
    serve("127.0.0.1", a.port)


if __name__ == "__main__":
    main()
