"""A ledger with nothing but a SimpleFIN connection: no personal.json, no plaid.json, no gws, no spreadsheet.

Runs the real sync script and the real dashboard as subprocesses against a temp folder and a local stand-in for
SimpleFIN Bridge (no outside request: the price fetch, the one step that needs the internet, is stubbed). The run
must end rc=0 with no problem lines, and every dashboard endpoint must answer 200 with the optional sections
empty or switched off.
"""
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SYNC = ROOT / "bin" / "wealth-ledger-sync"
SERVER = ROOT / "dashboard" / "server.py"
NOW = int(time.time())
DAY = 86400
SKIPPED = ("plaid: not set up, skipped", "not set up (no sheet_id in personal.json), skipped", "backups off")
ENDPOINTS = ("summary", "trend", "accounts", "holdings", "crypto", "cards", "spending", "drafts", "budget",
             "subscriptions", "rental", "inherited_ira")


def txn(i, days_ago, amount, desc):
    return {"id": f"t{i}", "posted": NOW - days_ago * DAY, "transacted_at": NOW - days_ago * DAY,
            "amount": str(amount), "description": desc, "pending": False}


def account(aid, name, balance, txns=(), **extra):
    return {"id": aid, "conn_id": "c1", "name": name, "currency": "USD", "balance": balance,
            "balance-date": NOW - 3600, "transactions": list(txns), **extra}


def accounts():
    return [
        account("A1", "Everyday Checking", "3210.55", [
            txn(1, 2, -54.20, "GROCERY MART #123"), txn(2, 3, 2500, "ACME CORP PAYROLL"),
            txn(3, 8, -300, "CARD PAYMENT THANK YOU")], **{"available-balance": "3200.00"}),
        account("A2", "High Yield Savings", "15000.00", [txn(1, 10, 12.5, "Interest Paid")]),
        account("A3", "Rewards Visa", "-420.10", [
            txn(1, 1, -15.99, "STREAMFLIX"), txn(2, 4, -88.01, "GAS STATION 44"),
            txn(3, 8, 300, "PAYMENT THANK YOU")], **{"available-balance": "4579.90"}),
        account("A4", "Individual Brokerage", "52000.00", holdings=[
            {"id": "h1", "symbol": "VTI", "description": "Total Market ETF", "shares": "150",
             "market_value": "45000.00", "cost_basis": "0", "currency": "USD"},
            {"id": "h2", "symbol": "BND", "description": "Total Bond ETF", "shares": "100",
             "market_value": "7000.00", "cost_basis": "0", "currency": "USD"}]),
    ]


NET_WORTH = 3210.55 + 15000 + 52000 - 420.10


@pytest.fixture
def bridge():
    """A local SimpleFIN Bridge: GET <anything>/accounts answers with bridge.accounts."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"errlist": [], "connections": [{"conn_id": "c1", "name": "Example Bank",
                                                                "org_id": "o1"}],
                               "accounts": srv.accounts}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.accounts = accounts()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


class Ledger:
    """A temp home holding only what the quick start creates: the config folder and, once claimed, the access URL."""

    def __init__(self, home: Path, bridge=None):
        self.home, self.conf, self.status = home, home / "conf", home / "status"
        self.conf.mkdir(mode=0o700)
        if bridge:
            access = self.conf / "simplefin_access_url"
            access.write_text(f"http://user:secret@127.0.0.1:{bridge.server_address[1]}/simplefin\n")
            access.chmod(0o600)
        # the sync's python: the real one, except that the price fetch (public internet) is a no-op
        self.py = home / "python"
        self.py.write_text(f'#!/bin/sh\n[ "$1 $2" = "-m insights.prices" ] && {{ echo "prices: stubbed"; exit 0; }}\n'
                           f'exec "{sys.executable}" "$@"\n')
        self.py.chmod(0o755)
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "WEALTH_LEDGER_CONFIG_DIR": str(self.conf),
                    "WEALTH_LEDGER_DB": str(home / "data" / "txns.db"), "WEALTH_LEDGER_STATUS": str(self.status),
                    "WEALTH_LEDGER_LOCK": str(home / "lock"), "WEALTH_LEDGER_EMAIL_STATE": str(home / "email"),
                    "WEALTH_LEDGER_PYTHON": str(self.py), "WEALTH_LEDGER_LIVE_PRICES": "off"}

    def sync(self):
        return subprocess.run(["bash", str(SYNC)], env=self.env, capture_output=True, text=True, timeout=120)

    def status_lines(self):
        return [ln for ln in self.status.read_text().splitlines() if ln]  # no problems = one blank line


class Dashboard:
    """dashboard/server.py on a free port, signed in."""

    def __init__(self, ledger: Ledger):
        env = {**ledger.env, "WEALTH_LEDGER_PYTHON": sys.executable}
        out = subprocess.run([sys.executable, str(SERVER), "set-password", "--username", "test", "--generate"],
                             env=env, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        password = out.stdout.split("New password (shown once): ")[1].split()[0]
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        self.proc = subprocess.Popen([sys.executable, str(SERVER), "--port", str(port), "--no-tailscale"], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                assert self.proc.poll() is None, self.proc.stdout.read()
                time.sleep(0.1)
        form = urllib.parse.urlencode({"username": "test", "password": password}).encode()
        req = urllib.request.Request(self.base + "/login", data=form, headers={"Origin": self.base})
        assert self.http.open(req, timeout=30).status == 200

    def get(self, path):
        r = self.http.open(self.base + path, timeout=60)
        return r.status, r.read()

    def api(self, name):
        status, body = self.get(f"/api/{name}")
        assert status == 200, name
        return json.loads(body)

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()


@pytest.fixture
def dashboard():
    started = []

    def start(ledger):
        started.append(Dashboard(ledger))
        return started[-1]

    yield start
    for d in started:
        d.stop()


def test_sync_ends_clean_with_every_add_on_skipped(tmp_path, bridge):
    led = Ledger(tmp_path, bridge)
    r = led.sync()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PROBLEM" not in r.stdout and "Traceback" not in r.stdout + r.stderr
    for line in SKIPPED:
        assert line in r.stdout
    assert not (led.conf / "personal.json").exists() and not (led.conf / "plaid.json").exists()
    (first,) = led.status_lines()  # one line: the run, no problems
    assert first.startswith("last run 20") and first.endswith(" rc=0")
    r = led.sync()  # and the daily run after it
    assert r.returncode == 0 and "PROBLEM" not in r.stdout and len(led.status_lines()) == 1


def test_account_with_an_unguessable_name_is_reported_until_account_kinds_names_it(tmp_path, bridge):
    bridge.accounts.append(account("A5", "360 Performance", "900.00"))
    led = Ledger(tmp_path, bridge)
    r = led.sync()
    assert r.returncode == 0
    _, problem = led.status_lines()
    assert '"360 Performance" has no type yet' in problem and "account_kinds" in problem
    (led.conf / "personal.json").write_text(json.dumps({"account_kinds": {"360 Performance%": "savings"}}))
    r = led.sync()
    assert r.returncode == 0 and "PROBLEM" not in r.stdout, r.stdout
    assert len(led.status_lines()) == 1


def test_dashboard_serves_a_synced_simplefin_only_ledger(tmp_path, bridge, dashboard):
    led = Ledger(tmp_path, bridge)
    assert led.sync().returncode == 0
    d = dashboard(led)
    replies = {name: d.api(name) for name in ENDPOINTS}
    assert isinstance(d.api("last"), dict)  # the saved copy of the last good replies

    s = replies["summary"]
    assert s["networth_error"] is None and s["sheetless"] is True
    assert s["status"]["rc"] == 0 and s["status"]["problems"] == []
    assert sorted(s["hide"]) == ["budget", "cryptocard", "ira", "rentalcard"]
    assert s["net_worth"] == pytest.approx(NET_WORTH) and s["liabilities"] == pytest.approx(420.10)
    assert s["sheet_net_worth"] is None and s["sheet_only_total"] == 0
    by_cat = {c["key"]: c["value"] for c in s["categories"]}
    assert by_cat["cash"] == pytest.approx(18210.55) and by_cat["taxable"] == pytest.approx(52000)
    assert by_cat.get("other", 0) == 0  # every synced account lands in a named category

    assert len(replies["accounts"]["accounts"]) == 4
    assert replies["holdings"]["total"] == pytest.approx(52000) and replies["holdings"]["count"] == 2
    cards = replies["cards"]
    assert len(cards["cards"]) == 1 and cards["total"] == pytest.approx(420.10)
    month = replies["spending"]["monthly"]
    assert month and sum(m["spent"] for m in month) == pytest.approx(54.20 + 15.99 + 88.01)  # no card payment
    assert sum(m["income"] for m in month) == pytest.approx(2512.5)
    assert len(replies["trend"]["series"]) == 1 and replies["trend"]["sheet_points"] == []
    assert replies["budget"]["off"] is True and replies["budget"]["error"] is None
    assert replies["crypto"] == {"total": None, "wallets": [], "coins": [], "transfers": []}
    assert replies["rental"]["items"] == [] and len(replies["rental"]["months"]) == 1
    assert replies["drafts"]["warnings"] == [] and replies["subscriptions"]["unusual"] == []

    status, page = d.get("/")
    assert status == 200 and b"<html" in page
    assert d.get("/icons/example-bank.png") == (204, b"")  # no icon file: empty, not an error


def test_dashboard_started_before_the_first_sync(tmp_path, dashboard):
    led = Ledger(tmp_path)  # nothing claimed, no database, no status file
    d = dashboard(led)
    assert os.path.exists(led.env["WEALTH_LEDGER_DB"])  # an empty ledger was created
    replies = {name: d.api(name) for name in ENDPOINTS}
    assert isinstance(d.api("last"), dict)  # the saved copy of the last good replies
    s = replies["summary"]
    assert s["networth_error"] is None and s["net_worth"] == 0
    (problem,) = s["status"]["problems"]
    assert problem.startswith("No sync has run yet")
    assert replies["accounts"]["accounts"] == [] and replies["cards"]["cards"] == []
    assert replies["holdings"]["holdings"] == [] and replies["trend"]["series"] == []
    assert d.get("/")[0] == 200


# ---------- the optional pieces, one at a time ----------
def test_personal_json_is_optional(tmp_path, monkeypatch):
    import ctdb
    import personal
    monkeypatch.delenv("WEALTH_LEDGER_PERSONAL")
    monkeypatch.setenv("WEALTH_LEDGER_CONFIG_DIR", str(tmp_path))
    assert personal.path() == tmp_path / "personal.json" and personal.load() == {}
    assert personal.section("dashboard") == {} and not personal.has_sheet()
    assert personal.auto_kind() is True  # nobody classifies accounts by hand: guess them
    assert ctdb.personal_account_kinds() == [] and ctdb.personal_credit_limits() == []
    with pytest.raises(personal.PersonalConfigError):
        personal.sheet_id()


def test_auto_kind_defaults_off_with_a_spreadsheet_and_can_be_set(tmp_path, monkeypatch):
    import personal
    for i, (cfg, want) in enumerate([({"sheet_id": "x"}, False), ({"sheet_id": "x", "auto_kind": True}, True),
                                     ({"auto_kind": False}, False), ({}, True)]):
        f = tmp_path / f"p{i}.json"
        f.write_text(json.dumps(cfg))
        monkeypatch.setenv("WEALTH_LEDGER_PERSONAL", str(f))
        assert personal.auto_kind() is want, cfg


def test_backup_and_institution_check_are_off_without_personal_json(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "WEALTH_LEDGER_CONFIG_DIR": str(tmp_path / "conf")}
    for script, said in (("wealth-ledger-backup", "backups off"), ("simplefin-institution-check", "nothing to watch")):
        r = subprocess.run([str(ROOT / "bin" / script)], env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and said in r.stdout, (script, r.stdout, r.stderr)
    assert list(tmp_path.iterdir()) == []  # and nothing was written


def test_valuations_skip_providers_that_are_not_set_up(tmp_path):
    """No "home" or "cars" in personal.json: those providers say so and the run still ends rc=0."""
    cfg = tmp_path / "personal.json"
    cfg.write_text("{}")
    code = ("import sys; sys.path.insert(0, %r); from valuations import run; "
            "print(sorted(m.NAME for m in run.providers()))" % str(ROOT))
    r = subprocess.run([sys.executable, "-c", code], env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
                                                          "WEALTH_LEDGER_PERSONAL": str(cfg)},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "home: not set up, skipped" in r.stdout and "cars: not set up, skipped" in r.stdout
    assert "'home'" not in r.stdout.splitlines()[-1] and "'metals'" in r.stdout.splitlines()[-1]
