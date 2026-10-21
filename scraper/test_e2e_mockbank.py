"""End-to-end: BankScraper vs the local MockBank (headless). Run: .venv/bin/pytest -q scraper/"""
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from bank_scraper import (BankScraper, Credentials, FileDropOtpProvider, WebhookOtpProvider,  # noqa: E402
                          extract_code, parse_amount)
from mockbank.server import PASSWORD, USER, MockBank  # noqa: E402


class MockBankScraper(BankScraper):
    bank = "mockbank"
    csv_columns = {"txn_date": "Transaction Date", "post_date": "Post Date", "description": "Description",
                   "category": "Category", "amount": "Amount"}

    def __init__(self, base_url, **kw):
        super().__init__(**kw)
        self.login_url, self.activity_url = base_url + "/login", base_url + "/activity"

    def is_logged_in(self, page):
        return page.locator("#download-csv").is_visible()

    def login(self, page, creds):
        page.fill("#user", creds.username)
        page.fill("#pass", creds.password)
        page.click("#signin")
        page.wait_for_load_state("domcontentloaded")

    def needs_otp(self, page):
        return page.locator("#otp").is_visible()

    def submit_otp(self, page, code):
        page.fill("#otp", code)
        page.click("#otp-submit")
        page.wait_for_load_state("domcontentloaded")

    def trigger_download(self, page):
        page.click("#download-csv")


def relay_sms(sms_file: Path, drop: Path, stop: threading.Event):
    """Plays the role of android-sms-gateway -> webhook handler -> file drop."""
    while not stop.is_set():
        if sms_file.exists():
            drop.write_text(sms_file.read_text())
            sms_file.unlink()
        time.sleep(0.1)


@pytest.fixture
def env(tmp_path):
    sms = tmp_path / "sms.txt"
    bank = MockBank(sms).start()
    stop = threading.Event()
    drop = tmp_path / "otp.txt"
    threading.Thread(target=relay_sms, args=(sms, drop, stop), daemon=True).start()
    yield bank, drop, tmp_path
    stop.set()
    bank.stop()


def mk(env, **kw):
    bank, drop, tmp = env
    return MockBankScraper(bank.url, otp=FileDropOtpProvider(drop, timeout=20), db_path=tmp / "txns.db",
                           profiles_root=tmp / "profiles", creds=Credentials(USER, PASSWORD), **kw)


def rows(tmp):
    with sqlite3.connect(tmp / "txns.db") as db:
        return db.execute("select description, amount_cents from txns order by txn_date, amount_cents").fetchall()


def test_login_otp_download_then_session_reuse(env):
    bank, drop, tmp = env
    s1 = mk(env)
    assert s1.run() == 4
    assert (s1.stats["logins"], s1.stats["otps"]) == (1, 1)
    assert oct(s1.profile_dir.stat().st_mode & 0o777) == "0o700"
    assert rows(tmp) == [("PAYMENT THANK YOU", 50000), ("AMAZON MKTPLACE", -4210),
                         ("BLUE BOTTLE COFFEE", -575), ("BLUE BOTTLE COFFEE", -575)]  # dup coffee kept

    # run 2: persistent profile -> neither login nor OTP; upsert is idempotent
    s2 = mk(env)
    s2.run()
    assert (s2.stats["logins"], s2.stats["otps"]) == (0, 0)
    assert len(rows(tmp)) == 4
    assert bank.stats == {"logins": 1, "otps": 1, "downloads": 2}

    # run 3: server drops the session but device cookie stays trusted -> login, NO otp
    urllib.request.urlopen(bank.url + "/admin/expire_sessions").read()
    s3 = mk(env)
    s3.run()
    assert (s3.stats["logins"], s3.stats["otps"]) == (1, 0)
    assert len(rows(tmp)) == 4


def test_webhook_provider_parses_gateway_payload():
    p = WebhookOtpProvider(port=0, timeout=5)
    p.prepare()
    body = b'{"event":"sms:received","payload":{"message":"Chase: code 482913 expires in 10 min","phoneNumber":"+1999"}}'
    threading.Timer(0.2, lambda: urllib.request.urlopen(
        urllib.request.Request(f"http://127.0.0.1:{p.port}/sms", data=body))).start()
    assert p.get_code("chase") == "482913"
    p.close()


def test_helpers():
    assert extract_code("Your code is 123456.") == "123456"
    assert parse_amount("(12.50)") == -1250 and parse_amount("$1,234.56") == 123456
