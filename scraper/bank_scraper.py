"""Local, Plaid-free bank scraper boilerplate (Playwright, persistent profiles).

Flow per bank:
    persistent context -> (already logged in? skip) -> login -> 2FA page? ->
    OtpProvider -> activity page -> page.expect_download (bank's own CSV/QFX) ->
    parse -> normalized records -> SQLite upsert.

Design rules
  * Prefer the bank's own export file over DOM-scraping tables.
  * Credentials come from the OS keyring or env vars, never from source.
  * One persistent browser profile per bank (0700) so "remember this device"
    cookies survive between runs.  Push approvals (Amex/Chase app) cannot be
    automated: raise ManualInterventionRequired and re-run headed (headless=False).
  * Amounts are stored as integer cents, charges negative.

This module never talks to a real bank unless you explicitly run a concrete
subclass (e.g. ChaseScraper) yourself.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Iterable, Optional

from playwright.sync_api import BrowserContext, Page, sync_playwright

APP = "wealth-ledger"
DATA_HOME = Path(os.environ.get("WEALTH_LEDGER_HOME", Path.home() / ".local/share" / APP))
DEFAULT_DB = DATA_HOME / "txns.db"
DEFAULT_PROFILES = DATA_HOME / "profiles"


# --------------------------------------------------------------------------- errors
class ScraperError(RuntimeError):
    pass


class LoginFailed(ScraperError):
    pass


class OtpTimeout(ScraperError):
    pass


class ManualInterventionRequired(ScraperError):
    """Push approval, CAPTCHA, 'unusual activity' page...  Re-run headed."""


# --------------------------------------------------------------------------- credentials
@dataclass
class Credentials:
    username: str
    password: str

    def __repr__(self) -> str:  # never leak into logs/tracebacks
        return f"Credentials(username={self.username!r}, password='***')"


def load_credentials(bank: str) -> Credentials:
    """Keyring first (service 'wealth-ledger', keys '<bank>:username' / '<bank>:password'),
    then env WEALTH_LEDGER_<BANK>_USER / _PASS.

    Store with:  python -m keyring set wealth-ledger chase:password
    """
    user = pw = None
    try:
        import keyring

        user = keyring.get_password(APP, f"{bank}:username")
        pw = keyring.get_password(APP, f"{bank}:password")
    except Exception:  # no keyring backend (headless server) -> env fallback
        pass
    env = f"WEALTH_LEDGER_{bank.upper()}"
    user = user or os.environ.get(f"{env}_USER")
    pw = pw or os.environ.get(f"{env}_PASS")
    if not user or not pw:
        raise LoginFailed(f"No credentials for {bank!r}: set keyring {APP}/{bank}:username|password or ${env}_USER/_PASS")
    return Credentials(user, pw)


# --------------------------------------------------------------------------- OTP providers
class OtpProvider:
    """Pluggable one-time-code source. `prepare()` is called right BEFORE the
    login form is submitted (so stale codes can be discarded); `get_code()` blocks."""

    def prepare(self) -> None:
        pass

    def get_code(self, bank: str, hint: str = "") -> str:
        raise NotImplementedError


class ManualPrompt(OtpProvider):
    """Human-in-the-loop: type the code in the terminal."""

    def get_code(self, bank: str, hint: str = "") -> str:
        return input(f"[{bank}] enter OTP ({hint}): ").strip()


_CODE_RE = re.compile(r"(?<!\d)(\d{6,8}|\d{4,5})(?!\d)")


def extract_code(text: str) -> Optional[str]:
    """Pull an OTP out of free text. Prefers 6-8 digits, e.g. 'Your code is 123456'."""
    m = _CODE_RE.search(text or "")
    return m.group(1) if m else None


class FileDropOtpProvider(OtpProvider):
    """Poll a file; anything that writes a code (or an SMS body) into it works
    (cron, a webhook handler, `echo 123456 > file`). The file is deleted once read."""

    def __init__(self, path: Path, timeout: float = 180, poll: float = 0.25):
        self.path, self.timeout, self.poll = Path(path), timeout, poll

    def prepare(self) -> None:
        self.path.unlink(missing_ok=True)  # discard stale codes

    def get_code(self, bank: str, hint: str = "") -> str:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.path.exists():
                code = extract_code(self.path.read_text())
                if code:
                    self.path.unlink(missing_ok=True)
                    return code
            time.sleep(self.poll)
        raise OtpTimeout(f"no OTP appeared in {self.path} within {self.timeout}s")


class WebhookOtpProvider(OtpProvider):
    """Tiny localhost HTTP receiver for the android-sms-gateway `sms:received` webhook.

    Point the gateway webhook (or a relay) at http://<host>:<port>/sms. Payload shape
    {"event":"sms:received","payload":{"message":"...","phoneNumber":"..."}} - but the
    whole body is regex-searched, so other shapes work too. Optional `sender_filter`
    (substring of the body/phone) avoids grabbing unrelated codes."""

    def __init__(self, port: int = 8765, host: str = "127.0.0.1", timeout: float = 180,
                 sender_filter: Optional[str] = None):
        self.host, self.port, self.timeout, self.sender_filter = host, port, timeout, sender_filter
        self._code: Optional[str] = None
        self._cv = threading.Condition()
        self._server: Optional[HTTPServer] = None

    def _start(self) -> None:
        if self._server:
            return
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf8", "replace")
                try:
                    msg = json.loads(body).get("payload", {}).get("message") or body
                except Exception:
                    msg = body
                ok = not outer.sender_filter or outer.sender_filter.lower() in body.lower()
                code = extract_code(msg) if ok else None
                if code:
                    with outer._cv:
                        outer._code = code
                        outer._cv.notify_all()
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):  # silence
                pass

        self._server = HTTPServer((self.host, self.port), H)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def prepare(self) -> None:
        self._start()
        with self._cv:
            self._code = None

    def get_code(self, bank: str, hint: str = "") -> str:
        self._start()
        with self._cv:
            if not self._cv.wait_for(lambda: self._code, timeout=self.timeout):
                raise OtpTimeout("no SMS webhook received in time")
            code, self._code = self._code, None
            return code

    def close(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None


class GmailOtpProvider(OtpProvider):
    """STUB. Intended: Gmail API (users.messages.list q='from:<bank> newer_than:5m'
    -> get -> extract_code(snippet)), polled every few seconds after prepare() records
    the start timestamp. Needs an OAuth token with gmail.readonly. Not implemented."""

    def __init__(self, query: str = "newer_than:5m (code OR verification)"):
        self.query = query

    def get_code(self, bank: str, hint: str = "") -> str:
        raise NotImplementedError("Wire up the Gmail API here (gmail.readonly scope).")


# --------------------------------------------------------------------------- records + CSV
@dataclass
class Txn:
    bank: str
    account: str
    txn_date: str          # ISO yyyy-mm-dd
    post_date: str
    description: str
    category: str
    amount_cents: int      # charges negative
    txn_id: str = ""
    raw: dict = field(default_factory=dict)


def parse_amount(s: str) -> int:
    s = (s or "").strip().replace("$", "").replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        cents = int((Decimal(s) * 100).to_integral_value())
    except InvalidOperation:
        raise ValueError(f"bad amount {s!r}")
    return -cents if neg else cents


def parse_date(s: str) -> str:
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def parse_csv(text: str, bank: str, account: str, columns: dict[str, str],
              negate: bool = False) -> list[Txn]:
    """Generic mapper. `columns` maps our field -> CSV header, with keys
    txn_date, post_date, description, category, amount (optional: txn_id).
    Set negate=True for banks that export charges as positive numbers (Amex)."""
    out: list[Txn] = []
    seen: dict[str, int] = {}
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        g = lambda k: (row.get(columns.get(k, ""), "") or "").strip()  # noqa: E731
        if not g("amount"):
            continue
        amt = parse_amount(g("amount"))
        t = Txn(bank, account, parse_date(g("txn_date")), parse_date(g("post_date")),
                g("description"), g("category"), -amt if negate else amt, g("txn_id"), dict(row))
        # Stable id: bank-provided id if any, else hash + occurrence index so two
        # identical coffees on one day do not collapse into one row.
        if not t.txn_id:
            key = "|".join([bank, account, t.txn_date, t.description, str(t.amount_cents)])
            n = seen[key] = seen.get(key, 0) + 1
            t.txn_id = hashlib.sha1(f"{key}|{n}".encode()).hexdigest()[:20]
        out.append(t)
    return out


# --------------------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS txns(
  txn_id TEXT PRIMARY KEY, bank TEXT NOT NULL, account TEXT, txn_date TEXT, post_date TEXT,
  description TEXT, category TEXT, amount_cents INTEGER NOT NULL, raw TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS txns_date ON txns(txn_date);
"""


def upsert(db_path: Path, txns: Iterable[Txn]) -> int:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    with sqlite3.connect(db_path) as db:
        db.executescript(SCHEMA)
        for t in txns:
            db.execute(
                """INSERT INTO txns VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(txn_id) DO UPDATE SET description=excluded.description,
                     category=excluded.category, post_date=excluded.post_date,
                     amount_cents=excluded.amount_cents, raw=excluded.raw, last_seen=excluded.last_seen""",
                (t.txn_id, t.bank, t.account, t.txn_date, t.post_date, t.description,
                 t.category, t.amount_cents, json.dumps(t.raw), now, now))
            n += 1
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    return n


# --------------------------------------------------------------------------- base scraper
class BankScraper:
    """Subclass and fill in the bank-specific hooks. Selectors live in class
    attributes / methods marked `# MAINTAIN:` - those are what break when the bank
    redesigns its site."""

    bank = "base"
    login_url = ""
    activity_url = ""
    csv_columns: dict[str, str] = {}
    csv_negate = False
    account_label = "default"

    def __init__(self, otp: Optional[OtpProvider] = None, *, headless: bool = True,
                 db_path: Path = DEFAULT_DB, profiles_root: Path = DEFAULT_PROFILES,
                 creds: Optional[Credentials] = None, channel: Optional[str] = None,
                 timeout_ms: int = 30_000):
        self.otp = otp or ManualPrompt()
        self.headless, self.db_path, self.channel, self.timeout_ms = headless, Path(db_path), channel, timeout_ms
        self.profile_dir = Path(profiles_root) / self.bank
        self._creds = creds
        self.stats = {"logins": 0, "otps": 0}

    # ---- hooks to implement ------------------------------------------------
    def is_logged_in(self, page: Page) -> bool:        # MAINTAIN
        raise NotImplementedError

    def login(self, page: Page, creds: Credentials) -> None:   # MAINTAIN
        raise NotImplementedError

    def needs_otp(self, page: Page) -> bool:           # MAINTAIN
        raise NotImplementedError

    def submit_otp(self, page: Page, code: str) -> None:       # MAINTAIN
        raise NotImplementedError

    def needs_push_approval(self, page: Page) -> bool:  # MAINTAIN (optional)
        return False

    def trigger_download(self, page: Page) -> None:    # MAINTAIN - click the export button
        raise NotImplementedError

    def parse(self, text: str) -> list[Txn]:
        return parse_csv(text, self.bank, self.account_label, self.csv_columns, self.csv_negate)

    # ---- machinery ---------------------------------------------------------
    def _ensure_profile_dir(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir's mode is umask-filtered and only applies to the leaf; enforce on all.
        for p in (self.profile_dir, self.profile_dir.parent):
            os.chmod(p, stat.S_IRWXU)

    def _open(self, pw) -> BrowserContext:
        self._ensure_profile_dir()
        kw = dict(headless=self.headless, accept_downloads=True, viewport={"width": 1280, "height": 900})
        if self.channel:  # e.g. "chrome": real Chrome has a more bank-friendly TLS/fingerprint
            kw["channel"] = self.channel
        ctx = pw.chromium.launch_persistent_context(str(self.profile_dir), **kw)
        ctx.set_default_timeout(self.timeout_ms)
        return ctx

    def _authenticate(self, page: Page) -> None:
        page.goto(self.activity_url)
        page.wait_for_load_state("domcontentloaded")
        if self.is_logged_in(page):
            return  # persistent session still valid: no login, no OTP
        page.goto(self.login_url)
        creds = self._creds or load_credentials(self.bank)
        self.otp.prepare()
        self.stats["logins"] += 1
        self.login(page, creds)
        if self.needs_push_approval(page):
            raise ManualInterventionRequired(f"{self.bank}: app push approval needed; run with headless=False")
        if self.needs_otp(page):
            self.stats["otps"] += 1
            code = self.otp.get_code(self.bank, hint=f"{self.bank} 2FA")
            self.submit_otp(page, code)
        page.goto(self.activity_url)
        if not self.is_logged_in(page):
            raise LoginFailed(f"{self.bank}: not logged in after login/OTP (bad creds, CAPTCHA, or unusual-activity page)")

    def fetch_csv(self) -> str:
        with sync_playwright() as pw:
            ctx = self._open(pw)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    self._authenticate(page)
                    with page.expect_download() as dl:
                        self.trigger_download(page)
                    path = dl.value.path()  # temp file managed by Playwright
                    return Path(path).read_text(encoding="utf-8-sig")
                except Exception:
                    self._debug_screenshot(page)
                    raise
            finally:
                ctx.close()

    def _debug_screenshot(self, page: Page) -> None:
        try:
            page.screenshot(path=str(self.profile_dir.parent / f"{self.bank}-last-error.png"))
        except Exception:
            pass

    def run(self) -> int:
        txns = self.parse(self.fetch_csv())
        return upsert(self.db_path, txns)


# --------------------------------------------------------------------------- concrete example
class ChaseScraper(BankScraper):
    """SKELETON ONLY - selectors are illustrative and WILL need maintenance; Chase
    changes its markup often, and uses Akamai bot management (consider channel='chrome',
    headed first run, and never hammering: one run per day is plenty).
    Never run against a real account without reading Chase's Digital Services Agreement."""

    bank = "chase"
    login_url = "https://secure.chase.com/web/auth/dashboard"      # MAINTAIN
    activity_url = "https://secure.chase.com/web/auth/dashboard#/dashboard/overview"  # MAINTAIN: per-card activity URL
    csv_columns = {"txn_date": "Transaction Date", "post_date": "Post Date", "description": "Description",
                   "category": "Category", "amount": "Amount"}   # Chase CSV: charges negative

    # MAINTAIN: all selectors below. Chase renders the login form inside an iframe.
    SEL_LOGIN_FRAME = "iframe#logonbox"
    SEL_USER = "#userInput-input"
    SEL_PASS = "#passwordInput-input"
    SEL_SUBMIT = "#signin-button"
    SEL_OTP_CHOICE = "text=Get a code by text"          # delivery-method chooser page
    SEL_OTP_INPUT = "#otpInput-input"
    SEL_OTP_SUBMIT = "#log_on_to_landing_page-security-code-next"
    SEL_PUSH_HINT = "text=/approve.*(Chase|mobile) app/i"
    SEL_LOGGED_IN = "[data-testid='accounts-list'], #accountTiles"
    SEL_DOWNLOAD_ICON = "#downloadActivityIcon"
    SEL_FORMAT = "#downloadFileTypeOption"              # choose CSV
    SEL_DOWNLOAD_BTN = "#activityDownloadButton"

    def is_logged_in(self, page: Page) -> bool:
        return page.locator(self.SEL_LOGGED_IN).first.is_visible()

    def login(self, page: Page, creds: Credentials) -> None:
        f = page.frame_locator(self.SEL_LOGIN_FRAME)
        f.locator(self.SEL_USER).fill(creds.username)
        f.locator(self.SEL_PASS).fill(creds.password)
        f.locator(self.SEL_SUBMIT).click()
        page.wait_for_load_state("networkidle")

    def needs_push_approval(self, page: Page) -> bool:
        return page.locator(self.SEL_PUSH_HINT).first.is_visible()

    def needs_otp(self, page: Page) -> bool:
        choice = page.locator(self.SEL_OTP_CHOICE).first
        if choice.is_visible():
            choice.click()  # ask for the SMS code (hits the android-sms-gateway)
        return page.locator(self.SEL_OTP_INPUT).first.is_visible()

    def submit_otp(self, page: Page, code: str) -> None:
        page.locator(self.SEL_OTP_INPUT).fill(code)
        page.locator(self.SEL_OTP_SUBMIT).click()
        page.wait_for_load_state("networkidle")

    def trigger_download(self, page: Page) -> None:
        page.locator(self.SEL_DOWNLOAD_ICON).click()
        page.locator(self.SEL_FORMAT).select_option(label="Spreadsheet (Excel, CSV)")
        page.locator(self.SEL_DOWNLOAD_BTN).click()


if __name__ == "__main__":  # pragma: no cover
    import sys

    cls = {"chase": ChaseScraper}[sys.argv[1] if len(sys.argv) > 1 else "chase"]
    print("rows upserted:", cls(headless="--headed" not in sys.argv).run())
