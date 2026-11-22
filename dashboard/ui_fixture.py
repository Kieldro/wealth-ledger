#!/usr/bin/env python3
"""A throwaway dashboard on made-up data: what the browser tests (test_ui.py) run against, and a way to look at the
page without a real ledger.

    .venv/bin/python dashboard/ui_fixture.py [--simplefin-only] [--port N]     # prints the URL and the login

Instance(folder) writes everything one dashboard needs under that folder (a synthetic ledger, a personal.json, a
status file, a login) and starts the real dashboard/server.py on a free port with every WEALTH_LEDGER_* path, and
HOME itself, pointed into the folder, so nothing of the owner's can be read. Nothing leaves the machine either:
first on its PATH are a stand-in `gws` that answers the two spreadsheet reads from canned rows and a stand-in
`systemd-run` that refuses (so a sync is a child of this dashboard, never a unit of the user's systemd), and
"Sync now" runs a stand-in script that rewrites the status file and calls no bank.

Every name, balance and transaction below is invented; amounts are round so the tests can state the totals.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SERVER = HERE / "server.py"
USER = "tester"
DAYS = 45  # daily balance snapshots, ending today

# id, name, institution, kind, balance today (cards and loans negative = owed, as the feeds store them)
ACCOUNTS = [
    ("chk", "Example Bank Checking (0000)", "Example Bank", "checking", 6400.0),
    ("sav", "Example Bank Savings (1111)", "Example Bank", "savings", 21000.0),
    ("card", "Example Rewards Card (2222)", "Example Card Co", "credit", -1350.0),
    ("brk", "Individual Brokerage (3333)", "Example Brokerage", "investment", 84000.0),
    ("roth", "ROTH IRA (4444)", "Example Brokerage", "retirement", 52000.0),
    ("inh", "Example Inherited IRA - BDA (5555)", "Example Brokerage", "retirement", 118000.0),
    ("mort", "MORTGAGE LOAN (6666)", "Example Lender", "loan", -212000.0),
]
# account id -> (symbol, description, shares, price); each account's positions add up to its balance
HOLDINGS = {
    "brk": [("VTI", "Total Market Fund", 200, 300.0), ("FBTC", "Bitcoin Fund", 100, 90.0), ("SPAXX", "Money Market", 15000, 1.0)],
    "roth": [("VXUS", "International Fund", 500, 64.0), ("BND", "Bond Fund", 250, 80.0)],
    "inh": [("VTI", "Total Market Fund", 300, 300.0), ("BND", "Bond Fund", 350, 80.0)],
}
# what the spreadsheet adds to the synced accounts: typed-in rows, and rows the valuations table overrides
CASH, CAR, HOME, CRYPTO, METALS = 1200.0, 14000.0, 350000.0, 30500.0, 9000.0
SYNCED_ASSETS = sum(a[4] for a in ACCOUNTS if a[4] > 0)
LIABILITIES = -sum(a[4] for a in ACCOUNTS if a[4] < 0)
ASSETS = SYNCED_ASSETS + CASH + CAR + HOME + CRYPTO + METALS  # the sheet-backed instance
BALANCE_SHEET = [  # the "balance sheet" tab: label, typed-in value (synced and valued rows are replaced)
    ["Assets"], ["Checking", 6000], ["Savings", 20000], ["Brokerage", 80000], ["Roth IRA", 50000],
    ["Inherited IRA", 115000], ["Cash", CASH], ["Total cryptocurrency (USD)", 30000], ["Total metals value", 8500],
    ["Home", 340000], ["Car", CAR], ["Liabilities"], ["Mortgage", 213000], ["Credit card", 1000],
    ["Total Equity", 440000],
]
INCOME_SHEET = [  # the "income sheet" tab: category, item, monthly budget
    ["Category", "Expense", "Monthly"], ["Home", "Mortgage", 1500], ["Bills", "Water", 100],
    ["Food", "Groceries", 400], ["Food", "Restaurants", 150], ["Car", "Gasoline", 120], ["", "Total", 2270],
]
PRICES = {"BTC": 100000.0, "XAU": 4000.0, "XAG": 50.0, "XPT": 1500.0, "HG": 5.0, "SPX": 6500.0, "IXIC": 21000.0}


def personal_json(today: date) -> dict:
    """The sheet-backed instance's personal.json (the SimpleFIN-only one has none)."""
    example = json.loads((ROOT / "personal.example.json").read_text())
    return {
        "sheet_id": "SYNTHETIC-SHEET",
        "networth": {
            "synced": {"Checking": ["Example Bank Checking (0000)"], "Savings": ["Example Bank Savings%"],
                       "Brokerage": ["Individual Brokerage%"], "Roth IRA": ["ROTH IRA%"],
                       "Inherited IRA": ["Example Inherited IRA%"], "Mortgage": ["MORTGAGE LOAN%"],
                       "Credit card": ["Example Rewards Card%"]},
            "asset_rows": ["Checking", "Savings", "Brokerage", "Roth IRA", "Inherited IRA", "Cash",
                           "Total cryptocurrency (USD)*", "Total metals value*", "Home", "Car"],
            "liab_rows": ["Mortgage", "Credit card"],
        },
        "credit_limits": {"Example Rewards Card%": 10000},
        "rules": [[10, "%EXAMPLE CARD PYMT%", "checking", "out", "card_payment", "checking-side card payment"]],
        "dashboard": {"ledger_start": _monthly(today, 1)[0].isoformat(), "feedback_email": "bugs@example.com",
                      **{k: example["dashboard"][k] for k in ("exact", "rules")}},
        "budget": {"rules": [["EXAMPLE CITY UTILITIES", "^bills/water"]]},
        "inherited_ira": {"account": "Example Inherited IRA%", "death_year": today.year - 2, "owner_started_rmds": True,
                          "beneficiary_birth_year": 1980, "growth_rate": 0.05},
        "rental": {"label": "Rental property",
                   "match": {"rent": [{"pattern": "%ZELLE%EXAMPLE TENANT%", "direction": "in"}],
                             "cleaning": ["%EXAMPLE CLEANING CO%"]}},
        "subscriptions": {"bill_patterns": ["EXAMPLE CITY UTILITIES", "EXAMPLE LENDER"]},
    }


# ---------- the ledger ----------
def _monthly(today: date, day: int, months: int = 6) -> list[date]:
    """`day` of each of the last `months` months, up to today, oldest first."""
    out, y, m = [], today.year, today.month
    for _ in range(months):
        d = date(y, m, day)
        if d <= today:
            out.append(d)
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out[::-1]


def _balance(kind: str, base: float, back: int) -> float:
    """An account's balance `back` days ago: its own wobble around a slow drift, exactly `base` today."""
    if kind in ("investment", "retirement"):
        return round(base * (1 + 0.011 * math.sin(0.9 * back) + 0.004 * math.sin(0.37 * back) - 0.0007 * back), 2)
    if kind == "loan":
        return round(base - 9.5 * back, 2)
    if kind == "credit":
        return round(base + 400 * math.sin(0.21 * back), 2)
    return round(base + 350 * math.sin(0.5 * back) - 6 * back, 2)


def _price(sym: str, back: int) -> float:
    k = sum(map(ord, sym))  # each symbol its own rhythm, so no two series move together
    return round(PRICES[sym] * (1 + 0.013 * math.sin(0.8 * back + k) + 0.006 * math.sin(0.29 * back + 2 * k) - 0.0004 * back), 4)


def build_ledger(path: str, sheet: bool, today: date | None = None) -> None:
    """Write the synthetic ledger. Run it in a process whose WEALTH_LEDGER_PERSONAL is the instance's own (ctdb seeds
    the rules and card limits from personal.json), which Instance.start() does."""
    sys.path.insert(0, str(ROOT))
    import ctdb
    today = today or date.today()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    db = ctdb.connect(path)
    for aid, name, inst, kind, base in ACCOUNTS:
        db.execute("INSERT OR REPLACE INTO account_kind VALUES (?,?,NULL)", (aid, kind))
        for back in range(DAYS):
            d = (today - timedelta(days=back)).isoformat()
            db.execute(ctdb.UPSERT_BALANCE, (d, "simplefin", aid, name, inst, str(_balance(kind, base, back)),
                                             None, "USD", d))
        for sym, desc, shares, price in HOLDINGS.get(aid, []):
            db.execute(ctdb.UPSERT_HOLDING, (today.isoformat(), "simplefin", aid, name, inst, sym, sym, desc,
                                             str(shares), str(price), str(shares * price), "0", "USD"))
    names = {a[0]: (a[1], a[2]) for a in ACCOUNTS}
    n = 0

    def txn(aid: str, d: date, amount: float, desc: str, pending: int = 0):
        nonlocal n
        n += 1
        db.execute(ctdb.upsert_sql("txns"), (f"t{n}", aid, *names[aid], d.isoformat(), None, f"{amount:.2f}", desc,
                                             None, None, pending, "USD", now, now, "simplefin"))

    for day, aid, amount, desc in (
            (1, "chk", 2450.0, "EXAMPLE EMPLOYER PAYROLL"), (15, "chk", 2450.0, "EXAMPLE EMPLOYER PAYROLL"),
            (3, "chk", -1480.0, "EXAMPLE LENDER MORTGAGE"), (3, "mort", 1480.0, "PAYMENT RECEIVED"),
            (5, "chk", 950.0, "ZELLE FROM EXAMPLE TENANT"), (6, "chk", -120.0, "EXAMPLE CLEANING CO"),
            (8, "chk", -92.0, "EXAMPLE CITY UTILITIES"), (20, "chk", -640.0, "EXAMPLE CARD PYMT"),
            (20, "card", 640.0, "PAYMENT THANK YOU"), (22, "chk", -300.0, "TRANSFER TO SAV"),
            (22, "sav", 300.0, "TRANSFER FROM CHK"), (28, "sav", 41.5, "INTEREST EARNED"),
            (12, "card", -15.99, "STREAMCO"), (12, "card", -10.99, "MUSICAPP"), (18, "card", -2.99, "CLOUDBOX STORAGE")):
        for d in _monthly(today, day):
            txn(aid, d, amount, desc)
    shops = (("KROGER #101", -72.4), ("STARBUCKS STORE 55", -6.85), ("SHELL OIL 0099", -41.2),
             ("TAQUERIA EXAMPLE", -28.0), ("EXAMPLE HARDWARE", -35.1))
    for k in range(50):  # card spending every third day, ending today, so every month on the page has some
        shop, amount = shops[k % len(shops)]
        txn("card", today - timedelta(days=3 * k), round(amount * (1 + 0.04 * (k % 4)), 2), shop)
    txn("card", today - timedelta(days=4), -1249.0, "EXAMPLE FURNITURE OUTLET")
    txn("card", today, -5.25, "EXAMPLE COFFEE ROASTERS", pending=1)
    txn("inh", today - timedelta(days=20), -4000.0, "PARTIAL DISTRIBUTION (Cash)")
    txn("brk", today - timedelta(days=10), 120.0, "DIVIDEND RECEIVED")

    for sym in PRICES:  # weekly closes for three years, daily for the last 120 days
        for back in [*range(120), *range(120, 1100, 7)]:
            db.execute("INSERT OR REPLACE INTO prices VALUES (?,?,?,?)",
                       ((today - timedelta(days=back)).isoformat(), sym, _price(sym, back), "synthetic"))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('prices_updated', ?)", (now,))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('inflation', ?)",
               (json.dumps({"pct": 3.1, "asof": today.isoformat()}),))
    if sheet:
        btc = PRICES["BTC"]
        crypto = {"holdings": [
            {"wallet": "Cold Wallet", "coin": "BTC", "qty": 0.25, "price": btc, "value": 25000.0, "qty_source": "onchain", "counted": True},
            {"wallet": "Kraken", "coin": "ETH", "qty": 1.5, "price": 3000.0, "value": 4500.0, "qty_source": "exchange-api", "counted": True},
            {"wallet": "Kraken", "coin": "BTC", "qty": 0.01, "price": btc, "value": 1000.0, "qty_source": "exchange-api", "counted": True}],
            "by_coin": {"BTC": {"qty": 0.26, "price": btc, "value": 26000.0}, "ETH": {"qty": 1.5, "price": 3000.0, "value": 4500.0}},
            "sheet_total": None, "warnings": [], "prices": {"BTC": {"usd": btc, "source": "synthetic"}}}
        metals = {"metals": {"gold": {"spot": PRICES["XAU"], "oz": 2, "source": "synthetic"},
                             "silver": {"spot": PRICES["XAG"], "oz": 20, "source": "synthetic"}}}
        for asset, value, source, detail in (
                ("Total cryptocurrency (USD)", CRYPTO, "qty: onchain/exchange-api", crypto),
                ("Total metals value", METALS, "spot x ounces", metals), ("Home", HOME, "synthetic model", None)):
            db.execute("INSERT OR REPLACE INTO valuations VALUES (?,?,?,?,?,?)",
                       (today.isoformat(), asset, value, source, json.dumps(detail) if detail else None, now))
        first = today - timedelta(days=DAYS - 1)
        for k in range(1, 9):  # the yearly spreadsheets' older net-worth points, before the daily snapshots
            d = (first - timedelta(days=91 * k)).isoformat()
            db.execute("INSERT OR REPLACE INTO networth_history VALUES (?,?,?,?,?,?)",
                       (d, 455000.0 - 9000.0 * k + 2500.0 * math.sin(k), None, None, "synthetic", now))
        for k, (direction, amount) in enumerate((("in", 0.05), ("out", 0.01))):
            db.execute("INSERT OR REPLACE INTO crypto_transfers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       ("Cold Wallet", "BTC", f"synthetic-tx-{k}", direction, (today - timedelta(days=30 + k)).isoformat(),
                        amount, 0.00001, "bc1q-synthetic-address", None, "synthetic memo" if k == 0 else None, "synthetic"))
    db.commit()
    db.close()


# ---------- stand-ins for the two programs the dashboard runs ----------
FAKE_GWS = '''#!{python}
"""Stand-in for the gws CLI: answers a spreadsheet read from canned rows. Calls nothing."""
import json, sys
args = sys.argv[1:]
try:
    rng = json.loads(args[args.index("--params") + 1])["range"]
    tabs = json.load(open({sheets!r}))
except (ValueError, KeyError, IndexError, OSError) as e:
    sys.exit(f"fake gws: unexpected call {{args}}: {{e!r}}")
tab = rng.split("!")[0]
if tab not in tabs:
    sys.exit(f"fake gws: no canned rows for {{rng}}")
print(json.dumps({{"range": rng, "values": tabs[tab]}}))
'''
FAKE_SYNC = '''#!{python}
"""Stand-in for bin/wealth-ledger-sync: holds the sync lock, waits while the hold file exists, then writes the status
file the way the real sync does (sync-plan.json may give an exit code and problem lines). Calls no bank."""
import fcntl, json, os, sys, time
from datetime import datetime
D = {folder!r}
lock = open(os.path.join(D, "sync.lock"), "w")
fcntl.flock(lock, fcntl.LOCK_EX)
give_up = time.time() + 30
while os.path.exists(os.path.join(D, "sync-hold")) and time.time() < give_up:
    time.sleep(0.02)
try:
    plan = json.load(open(os.path.join(D, "sync-plan.json")))
except OSError:
    plan = {{}}
rc = int(plan.get("rc", 0))
with open(os.path.join(D, "status.tmp"), "w") as f:
    f.write(f"last run {{datetime.now().astimezone().isoformat(timespec='seconds')}} rc={{rc}}\\n")
    f.writelines(p + "\\n" for p in plan.get("problems", []))
os.replace(os.path.join(D, "status.tmp"), os.path.join(D, "status"))
with open(os.path.join(D, "sync-runs"), "a") as f:
    f.write("run\\n")
sys.exit(rc)
'''


def _script(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Instance:
    """One dashboard on synthetic data. sheet=False is the SimpleFIN-only shape: no personal.json, no spreadsheet."""

    def __init__(self, folder: Path, sheet: bool = True, port: int | None = None):
        self.dir, self.sheet, self.port = Path(folder), sheet, port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.conf, self.bin = self.dir / "conf", self.dir / "bin"
        self.status, self.lock, self.log = self.dir / "status", self.dir / "sync.lock", self.dir / "server.log"
        self.password, self.proc = "", None
        d = str(self.dir)
        # nothing of the caller's WEALTH_LEDGER_* settings; HOME inside the folder so a path with no variable of its
        # own can't reach the owner's files; PATH without the real gws, tailscale or anything else to call out with
        self.env = {
            "HOME": str(self.dir / "home"), "PATH": f"{self.bin}:/usr/bin:/bin", "LANG": "C.UTF-8",
            "TZ": os.environ.get("TZ", ""), "PYTHONDONTWRITEBYTECODE": "1",
            "WEALTH_LEDGER_CONFIG_DIR": str(self.conf), "WEALTH_LEDGER_DB": f"{d}/ledger.db",
            "WEALTH_LEDGER_STATUS": str(self.status), "WEALTH_LEDGER_PERSONAL": str(self.conf / "personal.json"),
            "WEALTH_LEDGER_ACCESS": str(self.conf / "simplefin_access_url"), "WEALTH_LEDGER_LOCK": str(self.lock),
            "WEALTH_LEDGER_SYNC_LOG": f"{d}/sync.log", "WEALTH_LEDGER_SYNC_CMD": str(self.bin / "fake-sync"),
            "WEALTH_LEDGER_EMAIL_STATE": f"{d}/email-state.json", "PLAID_CONFIG": f"{d}/plaid.json",
            # live market prices from a file the tests write (set_live_markets), never the network; no file = off
            "WEALTH_LEDGER_LIVE_MARKETS": f"{d}/live-markets.json",
        }
        if not self.env["TZ"]:
            del self.env["TZ"]

    def _run(self, *args: str) -> str:
        r = subprocess.run([sys.executable, *args], env=self.env, capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise RuntimeError(f"{' '.join(args)} failed:\n{r.stdout}\n{r.stderr}")
        return r.stdout

    def start(self) -> "Instance":
        for p in (self.conf, self.bin, self.dir / "home"):
            p.mkdir(parents=True, exist_ok=True)
        if self.sheet:
            (self.conf / "personal.json").write_text(json.dumps(personal_json(date.today()), indent=1))
            (self.dir / "sheets.json").write_text(json.dumps({"balance sheet": BALANCE_SHEET, "income sheet": INCOME_SHEET}))
            _script(self.bin / "gws", FAKE_GWS.format(python=sys.executable, sheets=str(self.dir / "sheets.json")))
        _script(self.bin / "fake-sync", FAKE_SYNC.format(python=sys.executable, folder=str(self.dir)))
        # first on PATH: the dashboard then runs the sync as its own child instead of as a unit of the user's systemd
        _script(self.bin / "systemd-run", "#!/bin/sh\necho 'no systemd on the test instance' >&2\nexit 1\n")
        (self.conf / "simplefin_access_url").write_text("https://synthetic.invalid/simplefin\n")  # never read, never called
        self._run(str(Path(__file__).resolve()), "--build-ledger", self.env["WEALTH_LEDGER_DB"],
                  *([] if self.sheet else ["--simplefin-only"]))
        out = self._run(str(SERVER), "set-password", "--username", USER, "--generate")
        self.password = re.search(r"New password \(shown once\): (\S+)", out).group(1)
        self.reset()
        with open(self.log, "w") as log:
            self.proc = subprocess.Popen([sys.executable, str(SERVER), "--port", str(self.port), "--no-tailscale"],
                                         env=self.env, stdout=log, stderr=subprocess.STDOUT, cwd=str(self.dir))
        give_up = time.time() + 20
        while True:  # up once the port answers
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
                return self
            except OSError:
                if self.proc.poll() is not None or time.time() > give_up:
                    self.stop()
                    raise RuntimeError(f"the dashboard did not start:\n{self.log.read_text()}") from None
                time.sleep(0.05)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.proc = None

    # --- what the tests change between page loads
    def set_status(self, rc: int = 0, problems: tuple[str, ...] = (), age_min: float = 120) -> None:
        """The sync's status file: when it last finished (age_min minutes ago), its exit code, its problem lines."""
        when = (datetime.now().astimezone() - timedelta(minutes=age_min)).isoformat(timespec="seconds")
        tmp = self.dir / "status.new"
        tmp.write_text(f"last run {when} rc={rc}\n" + "".join(p + "\n" for p in problems))
        tmp.replace(self.status)

    def reset(self) -> None:
        """Back to the starting point: a clean sync two hours ago, no Sync now history, no sync held or planned, no live
        market prices (the strip shows the stored closes)."""
        self.set_status()
        for p in (self.conf / "dashboard_sync.json", self.dir / "sync-hold", self.dir / "sync-plan.json", self.dir / "sync-runs",
                  self.dir / "live-markets.json"):
            p.unlink(missing_ok=True)

    def set_live_markets(self, prices: dict | None) -> None:
        """What the markets strip's live sources answer now: {"BTC": usd, "XAU": usd, "SPX": usd} (a symbol left out =
        that source down); None = live prices off."""
        path = self.dir / "live-markets.json"
        if prices is None:
            path.unlink(missing_ok=True)
        else:
            tmp = self.dir / "live-markets.new"
            tmp.write_text(json.dumps(prices))
            tmp.replace(path)

    def hold_sync(self) -> None:
        """The next Sync now keeps running until release_sync()."""
        (self.dir / "sync-hold").touch()

    def release_sync(self) -> None:
        (self.dir / "sync-hold").unlink(missing_ok=True)

    def plan_sync(self, rc: int = 0, problems: tuple[str, ...] = ()) -> None:
        """How the next Sync now ends."""
        (self.dir / "sync-plan.json").write_text(json.dumps({"rc": rc, "problems": list(problems)}))

    def sync_runs(self) -> int:
        try:
            return len((self.dir / "sync-runs").read_text().splitlines())
        except OSError:
            return 0

    def sync_starts(self, minutes_ago: list[float]) -> None:
        """Pretend Sync now was pressed at these times (the server's own record of its starts)."""
        now = time.time()
        (self.conf / "dashboard_sync.json").write_text(json.dumps({"starts": [now - 60 * m for m in minutes_ago]}))


def main() -> int:
    ap = argparse.ArgumentParser(description="Serve the dashboard on synthetic data until Ctrl-C.")
    ap.add_argument("--simplefin-only", action="store_true", help="no personal.json, no spreadsheet")
    ap.add_argument("--port", type=int)
    ap.add_argument("--build-ledger", metavar="DB", help=argparse.SUPPRESS)  # what Instance.start() runs
    a = ap.parse_args()
    if a.build_ledger:
        build_ledger(a.build_ledger, sheet=not a.simplefin_only)
        return 0
    folder = Path(tempfile.mkdtemp(prefix="wealth-ledger-ui-"))
    inst = Instance(folder, sheet=not a.simplefin_only, port=a.port)
    try:
        inst.start()
        print(f"{inst.url}/   username {USER}   password {inst.password}   (synthetic data in {folder}; Ctrl-C to stop)")
        inst.proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        inst.stop()
        shutil.rmtree(folder, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
