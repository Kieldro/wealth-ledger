"""The public demo (/demo/): what it shows, and that it can never reach the real ledger, a real route or a session.

The isolation tests run a real dashboard (ui_fixture.Instance) whose own ledger is the "real" one here: its account
names and balances are the sentinels no demo answer may contain. Nothing reads the owner's files."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import unquote

import pytest

from dashboard import demo, peers, server
from dashboard import ui_fixture as F

HERE = Path(__file__).resolve().parent


# ---------- the made-up person ----------
@pytest.fixture(scope="module")
def built():
    return demo.build()


def test_demo_answers_every_page_route_and_each_accounts_transactions(built):
    routes = {r.removeprefix("/api/") for r in server.ROUTES} - {"account_txns"}
    assert routes <= set(built) and set(built["account_txns"]) == {a[0] for a in demo.ACCOUNTS}
    for name in routes:
        assert isinstance(json.loads(built[name]), dict), name
    txns = json.loads(built["account_txns"]["chk"])["txns"]
    assert len(txns) > 40 and any("PAYROLL" in t["description"] for t in txns)


def test_demo_person_is_near_the_median_for_his_age(built):
    s = json.loads(built["summary"])
    p25, median, p75 = peers.band_for(peers.NET_WORTH, demo.AGE)[2:5]
    assert p25 < s["net_worth"] < p75 and abs(s["net_worth"] - median) < 0.25 * median
    cats = {c["key"]: c["value"] for c in s["categories"]}
    assert cats["realestate"] == demo.HOME and cats["crypto"] > 0 and cats["metals"] > 0 and cats["mortgage"] < 0
    assert s["debt"]["mortgage"]["payoff"] and 0.035 < s["debt"]["mortgage"]["rate"] < 0.045
    assert s["networth_error"] is None and not s["status"]["problems"] and s["status"]["rc"] == 0
    assert set(s["hide"]) == {"ira", "rentalcard"}  # no inherited IRA or rental for this person
    trend = json.loads(built["trend"])
    assert len(trend["series"]) == demo.DAYS and trend["series"][-1]["date"] == date.today().isoformat()
    assert trend["sheet_points"] and not trend["valued"]["fallback"]
    assert json.loads(built["drafts"])["accounts"] and not json.loads(built["drafts"])["warnings"]
    spend = json.loads(built["spending"])["monthly"]
    assert len(spend) >= 4 and all(m["income"] > m["spent"] for m in spend if not m["partial"])
    assert json.loads(built["budget"])["months"] and json.loads(built["subscriptions"])["subscriptions"]
    assert {h["symbol"] for h in json.loads(built["holdings"])["holdings"]} >= {"VFFVX", "VTI", "VOO"}


def test_demo_is_the_same_each_time_for_a_day(built):
    again = demo.build()
    a, b = json.loads(built["summary"]), json.loads(again["summary"])
    assert (a["net_worth"], a["categories"]) == (b["net_worth"], b["categories"])
    assert built["spending"] == again["spending"] and built["holdings"] == again["holdings"]


def test_demo_dates_end_today_whatever_the_day():
    d = demo.build(date(2027, 3, 9), {"BTC": 120000.0})
    assert json.loads(d["trend"])["series"][-1]["date"] == "2027-03-09"
    assert json.loads(d["crypto"])["coins"][0]["price"] == 120000.0  # bitcoin at the price it was given


def test_demo_child_gets_an_environment_made_from_scratch(tmp_path, monkeypatch):
    for k in ("WEALTH_LEDGER_DB", "WEALTH_LEDGER_CONFIG_DIR", "WEALTH_LEDGER_PERSONAL", "GOOGLE_APPLICATION_CREDENTIALS",
              "PLAID_CONFIG", "SIMPLEFIN_ACCESS_URL"):
        monkeypatch.setenv(k, "/owner/secret/path")
    env = demo.sealed_env(tmp_path)
    assert "/owner/secret/path" not in json.dumps(env)
    assert env["HOME"].startswith(str(tmp_path)) and env["PATH"].startswith(f"{tmp_path}/bin:")
    for k, v in env.items():
        if k.startswith("WEALTH_LEDGER_") and k not in ("WEALTH_LEDGER_LIVE_MARKETS", "WEALTH_LEDGER_PYTHON", "WEALTH_LEDGER_SYNC_CMD"):
            assert v.startswith(str(tmp_path)), k
    assert env["WEALTH_LEDGER_LIVE_MARKETS"] == "off" and env["WEALTH_LEDGER_SYNC_CMD"] == "/bin/false"


def test_demo_answers_are_built_once_a_day_and_shared(monkeypatch):
    calls = []
    monkeypatch.setattr(demo, "build", lambda today, anchors: calls.append(today) or {"summary": b"{}", "account_txns": {}})
    d = demo.Demo()
    for _ in range(20):
        assert d.get(wait=5) == {"summary": b"{}", "account_txns": {}}
    assert calls == [date.today()]


def test_demo_keeps_serving_yesterdays_answers_while_todays_build(monkeypatch):
    d = demo.Demo()
    d._data, d._day = {"summary": b"old"}, "2000-01-01"
    started = []
    monkeypatch.setattr(d, "_build", lambda: started.append(1))
    monkeypatch.setattr(demo.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda self: target()})())
    assert d.get(wait=0)["summary"] == b"old" and started == [1]


def test_rate_limit_per_client():
    rl = server.RateLimit(3, 60)
    assert [rl.retry_after("a", now=100.0) for _ in range(3)] == [0, 0, 0]
    assert 1 <= rl.retry_after("a", now=101.0) <= 60 and rl.retry_after("b", now=101.0) == 0
    assert rl.retry_after("a", now=161.0) == 0  # the window moved on


# ---------- people your age ----------
def test_age_bands_follow_the_sources():
    nw, inc = peers.NET_WORTH, peers.INCOME
    assert peers.band_for(nw, 35)[:2] == [35, 44] and peers.band_for(nw, 44.9)[:2] == [35, 44]
    assert peers.band_for(nw, 34)[:2] == [0, 34] and peers.band_for(nw, 80)[:2] == [75, None]
    assert peers.band_for(inc, 22)[:2] == [15, 24] and peers.band_for(inc, 30)[:2] == [25, 34]
    assert peers.band_for(inc, 70)[:2] == [65, 74] and peers.band_for(inc, 14) is None
    assert [peers.band_label(r) for r in nw["bands"]] == ["under 35", "35-44", "45-54", "55-64", "65-74", "75+"]
    assert peers.band_for(nw, 35)[3] == 155_600 and peers.band_for(inc, 40)[2] == 114_100  # as published
    for rows in (nw["bands"], inc["bands"]):  # contiguous, no gaps or overlaps
        assert all(a[1] + 1 == b[0] for a, b in zip(rows, rows[1:])) and rows[-1][1] is None
    for r in nw["bands"]:
        assert r[2] < r[3] < r[4] < r[5]  # p25 < median < p75 < p90
    assert nw["url"].startswith("https://www.federalreserve.gov/") and inc["url"].startswith("https://www.census.gov/")


# ---------- report a bug ----------
def _body(href: str) -> str:
    return unquote(href.split("&body=", 1)[1])


def test_feedback_link_is_off_without_an_address(monkeypatch):
    monkeypatch.setattr(server.personal, "section", lambda name: {})
    assert server.feedback_href("dashboard") is None and server.feedback_link("signin") == ""
    monkeypatch.setattr(server.personal, "section", lambda name: {"feedback_email": "not an address <x>"})
    assert server.feedback_link("demo") == ""


def test_feedback_link_is_a_short_encoded_template_without_money(monkeypatch):
    monkeypatch.setattr(server.personal, "section", lambda name: {"feedback_email": "bugs+wl@example.com"})
    for page in ("signin", "dashboard", "demo"):
        href = server.feedback_href(page)
        assert href.startswith("mailto:bugs%2Bwl@example.com?subject=Wealth%20Ledger%20bug%20report")
        assert len(href) < server.FEEDBACK_MAX - 400  # room for the browser details the page adds
        assert " " not in href and "\n" not in href and "+" not in href.split("?", 1)[1]  # %20, never + (mail apps differ)
        body = _body(href)
        for line in ("What happened:", "What you expected:", "Steps to reproduce:", f"App version: {server.app_version()}",
                     "Browser: -", "Screen: -", "App: -", "Time: -"):
            assert line in body, line
        assert "$" not in body and not re.search(r"\d{3,}", body.replace(server.app_version(), ""))
    assert "(demo)" in unquote(server.feedback_href("demo")) and "Mode: demo" in _body(server.feedback_href("demo"))
    assert "(demo)" not in unquote(server.feedback_href("dashboard")) and "Mode: signed in" in _body(server.feedback_href("dashboard"))
    assert "Section: -" not in _body(server.feedback_href("signin"))
    assert 'href="mailto:' in server.feedback_link("dashboard") and "&amp;body=" in server.feedback_link("dashboard")


def test_feedback_script_fills_in_the_browser_details(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    script = ("const a = {href: %s};\n" % json.dumps("mailto:a@example.com?subject=S&body=" + _encoded_template())
              + "globalThis.document = {querySelectorAll: () => [], addEventListener: () => {}};\n"
              + "Object.defineProperty(globalThis, 'navigator', {value: {userAgent: 'TestAgent/1.0'}, configurable: true});\n"
              + "globalThis.screen = {width: 390, height: 844};\n"
              + "globalThis.innerWidth = 390; globalThis.innerHeight = 700; globalThis.window = {devicePixelRatio: 3};\n"
              + server.FEEDBACK_JS + "\nfbFill(a); console.log(a.href);\n")
    f = tmp_path / "fb.js"
    f.write_text(script)
    out = subprocess.run(["node", str(f)], capture_output=True, text=True, check=True).stdout.strip()
    body = unquote(out.split("&body=")[1])
    assert "Browser: TestAgent/1.0" in body and "Screen: 390x844 (window 390x700, pixel ratio 3)" in body
    assert "App: browser" in body and re.search(r"Time: \d{4}-\d\d-\d\d \d\d:\d\d UTC", body) and "Section: top of the page" in body
    assert len(out) < server.FEEDBACK_MAX and out.startswith("mailto:a@example.com?subject=S&body=")


def _encoded_template() -> str:
    from urllib.parse import quote
    return quote("What happened:\n\nApp version: abc\nSection: -\nBrowser: -\nScreen: -\nApp: -\nTime: -\n", safe="")


def test_demo_script_parses(tmp_path):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    parts = server.demo_parts()
    assert set(parts) == {"top", "peers", "script"}
    scripts = re.findall(r"<script>(.*?)</script>", parts["script"], re.S)
    assert len(scripts) == 1
    f = tmp_path / "demo.js"
    f.write_text(scripts[0])
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-600:]


def test_demo_page_has_the_banner_tag_and_no_placeholders(monkeypatch):
    monkeypatch.setattr(server, "markets_line", lambda wait=5.0: "")
    page = server.page_html("demo").decode()
    assert '<html lang="en" data-demo="1"' in page and "<title>Demo · Wealth Ledger</title>" in page
    assert 'class="demotag">DEMO</span></h1>' in page and "Demo mode: sample data for a fictional household, not real accounts." in page
    assert demo.NAME in page and not re.search(r"\{\{\w+\}\}", page) and 'id="peers"' in page and 'id="owndlg"' in page
    data = json.loads(re.search(r'<script type="application/json" id="demodata">(.*?)</script>', page, re.S).group(1))
    assert data["age"] == demo.AGE and data["peers"]["net_worth"]["bands"][1][3] == 155_600
    real = server.page_html("dashboard").decode()
    assert "data-demo" not in real.split(">", 2)[1] and 'id="demobar"' not in real and not re.search(r"\{\{\w+\}\}", real)
    csp = server.index_csp(page.encode())
    assert csp.count("'sha256-") == len(re.findall(r"<script>", page))  # the demo's script is allowed by its hash


# ---------- isolation, against a running dashboard ----------
@pytest.fixture(scope="module")
def inst():
    folder = Path(tempfile.mkdtemp(prefix="wealth-ledger-demo-test-"))
    i = F.Instance(folder)
    try:
        yield i.start()
    finally:
        i.stop()
        shutil.rmtree(folder, ignore_errors=True)


def call(inst, path, method="GET", cookie=None, data=None, headers=None):
    """(status, headers, body) without following redirects."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    h = {"Accept": "application/json", **(headers or {})}
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(inst.url + path, method=method, headers=h, data=data)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def login(inst) -> str:
    st, h, _ = call(inst, "/login", "POST", data=f"username={F.USER}&password={inst.password}".encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert st == 303
    return h["Set-Cookie"].split(";")[0]


REAL_GET = ["/", "/index.html", "/healthz", "/api/last", "/api/sync", "/icons/example-bank.png",
            *server.ROUTES, "/api/account_txns?id=chk"]
REAL_POST = ["/api/sync", "/api/refresh", "/api/connect"]
DEMO_GET = ["/demo/", *(f"/demo/api/{r.removeprefix('/api/')}" for r in server.ROUTES if r != "/api/account_txns"),
            *(f"/demo/api/account_txns?id={a[0]}" for a in demo.ACCOUNTS), "/demo/api/last", "/demo/api/sync"]


def test_demo_needs_no_session_and_sets_no_cookie(inst):
    st, h, body = call(inst, "/demo")
    assert st == 303 and h["Location"] == "/demo/"
    for path in DEMO_GET:
        st, h, body = call(inst, path)
        assert st == 200, path
        assert h.get("Set-Cookie") is None, path
    st, h, body = call(inst, "/login", headers={"Accept": "text/html"})
    assert st == 200 and b'href="/demo/"' in body and b"Try the demo" in body


def test_demo_answers_hold_nothing_of_the_real_ledger(inst):
    real_names = {a[1] for a in F.ACCOUNTS} | {a[2] for a in F.ACCOUNTS} | {h[1] for hs in F.HOLDINGS.values() for h in hs}
    # the real ledger's balances and totals (small round ones left out: those could turn up in the demo by chance)
    real_amounts = {f"{abs(a[4]):.1f}" for a in F.ACCOUNTS if abs(a[4]) >= 5000} | {
        f"{F.ASSETS:.1f}", f"{F.ASSETS - F.LIABILITIES:.1f}", f"{F.LIABILITIES:.1f}", f"{F.HOME:.1f}", f"{F.CRYPTO:.1f}"}
    real_words = ["EXAMPLE EMPLOYER", "EXAMPLE TENANT", "EXAMPLE CARD PYMT", "Cold Wallet", "Inherited", "STREAMCO",
                  "synthetic", str(inst.dir), "SYNTHETIC-SHEET"]
    everything = b""
    for path in DEMO_GET:
        st, _, body = call(inst, path)
        assert st == 200, path
        everything += body + b"\n"
    text = everything.decode()
    answers = text.split("</html>", 1)[1]  # the API answers alone, past the page (whose footer names the real files)
    for s in real_names:
        assert s not in text, s
    for s in real_words:  # the page's own text names an inherited IRA card; the answers must not
        assert s not in answers, s
    for amount in real_amounts:
        assert not re.search(r"(?<![\d.])" + re.escape(amount) + r"(?!\d)", answers), amount
    assert "Demo Bank Checking (0001)" in text and demo.NAME in text  # and it is the demo's data
    assert "/home/" not in answers and "/.config/" not in answers and "txns.db" not in answers and "/tmp" not in answers


def test_without_a_real_session_every_real_route_is_refused(inst):
    for cookie in (None, "ctd_session=demo", "ctd_demo=1", "ctd_session=v1." + "A" * 43 + ".9999999999." + "B" * 43):
        for path in REAL_GET:
            st, h, _ = call(inst, path, cookie=cookie)
            assert st in (401, 303), (path, cookie, st)
            if st == 303:
                assert h["Location"] == "/login", path
        for path in REAL_POST:
            st, _, _ = call(inst, path, "POST", cookie=cookie, data=b"")
            assert st == 401, (path, cookie, st)


def test_demo_changes_nothing_and_says_so(inst):
    runs = inst.sync_runs()
    for path in ("/demo/api/sync", "/demo/api/refresh", "/demo/api/connect", "/demo/api/layout", "/demo/", "/demo/api/summary"):
        st, _, body = call(inst, path, "POST", data=b"token=x")
        assert st == 403 and json.loads(body)["demo"] is True and "Not available in the demo" in json.loads(body)["error"], path
    assert inst.sync_runs() == runs
    st, _, body = call(inst, "/demo/api/sync")
    assert st == 200 and json.loads(body)["running"] is False
    for path in ("/demo/api/nothing", "/demo/api/_info", "/demo/api/_summary", "/demo/api/account_txns?id=nope",
                 "/demo/api/../api/summary", "/demo/icons/x.png"):
        assert call(inst, path)[0] == 404, path


def test_a_real_session_is_untouched_by_the_demo(inst):
    cookie = login(inst)
    assert call(inst, "/api/summary", cookie=cookie)[0] == 200
    for path in DEMO_GET[:3]:
        st, h, _ = call(inst, path, cookie=cookie)
        assert st == 200 and h.get("Set-Cookie") is None
    st, _, body = call(inst, "/demo/api/summary", cookie=cookie)
    assert "Example Bank" not in body.decode()  # signed in or not, the demo is the demo
    assert call(inst, "/api/summary", cookie=cookie)[0] == 200
