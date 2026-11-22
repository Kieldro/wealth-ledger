#!/usr/bin/env python3
"""The public demo: the whole dashboard on a made-up person, built without touching anything real.

Who: "Alex", a fictional single man of 35 with one job, a house, a car and median-ish money for his age (see
HOUSEHOLD; the pay is the Census median for men 35-44 working full-time, the net worth near the Fed's SCF median for
35-44, peers.py). Every name, account and transaction is invented; the merchants are ordinary national chains.

How (build()): a fresh temp folder gets a synthetic ledger (balances, holdings, ~4 months of transactions, three
years of prices), a personal.json, a status file and canned spreadsheet rows; then a child process started with an
environment made from scratch (HOME, PATH, every WEALTH_LEDGER_* path inside that folder, live market prices off, a
stand-in `gws` first on PATH) imports dashboard/server.py and asks every GET route of ROUTES for its answer, exactly
as the page would. Those answers, JSON bytes per route, are all the demo ever serves: server.py keeps them in memory
(Demo) and answers /demo/api/* from that dict, rebuilt once a day. Nothing in the child can name the owner's ledger,
config dir or sheet, and the parent never imports anything from it but the JSON.

Generated deterministically: a fixed seed, dates counted back from today (so the data always ends today). Only the
anchor prices (today's BTC, gold and S&P 500, from the public markets line when known) move the numbers.

    .venv/bin/python dashboard/demo.py --summary     # build once and print the headline numbers
"""
from __future__ import annotations

import json
import math
import os
import random
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SEED = 35
NAME = "Alex, 35"
AGE = 35
HOUSEHOLD = "Demo household"
DAYS = 130  # daily balances and transactions, ending today
BUILD_TIMEOUT_S = 120

# ---------- who ----------
SALARY = 82_000          # ~ Census median earnings, men 35-44 full-time year-round (peers.py)
NET_PAY = 2262.18        # biweekly, after tax, 401(k) 6%, HSA and health premium
K401_PER_CHECK = 315.38  # 6% of pay + 4% employer match
HSA_PER_CHECK = 100.0
MORTGAGE_RATE, MORTGAGE_PI = 0.04, 1312.91   # $275k 30-year at 4% (bought 2021)
AUTO_RATE, AUTO_PAYMENT = 0.059, 418.00

# id, name, institution, kind, balance today (cards and loans negative = owed, as the feeds store them)
ACCOUNTS = [
    ("chk", "Demo Bank Checking (0001)", "Demo Bank", "checking", 4820.0),
    ("sav", "Demo Bank High-Yield Savings (0002)", "Demo Bank", "savings", 9460.0),
    ("visa", "Demo Rewards Visa (0003)", "Demo Card Co", "credit", None),
    ("mc", "Demo Cash Back Mastercard (0004)", "Demo Card Co", "credit", None),
    ("k401", "Demo Employer 401(k) (0005)", "Demo Retirement Co", "retirement", 51800.0),
    ("roth", "Demo Roth IRA (0006)", "Demo Brokerage", "retirement", 14200.0),
    ("brk", "Demo Individual Brokerage (0007)", "Demo Brokerage", "investment", 8900.0),
    ("hsa", "Demo HSA (0008)", "Demo Brokerage", "hsa", 4450.0),
    ("mort", "Demo Home Mortgage (0009)", "Demo Mortgage Co", "loan", -252400.0),
    ("auto", "Demo Auto Loan (0010)", "Demo Credit Union", "loan", -13100.0),
]
CARD_LIMITS = {"visa": 9000.0, "mc": 4000.0}
CARD_CYCLE = {"visa": (22, 1350.0), "mc": (26, 420.0)}  # autopay day (the month before's statement), a usual month
# account -> [(symbol, description, share of the account)]; the rest of the balance is cash
FUNDS = {
    "k401": [("VFFVX", "Vanguard Target Retirement 2055 Fund", 1.0)],
    "roth": [("VTI", "Vanguard Total Stock Market ETF", 0.8), ("VXUS", "Vanguard Total International Stock ETF", 0.2)],
    "brk": [("VOO", "Vanguard S&P 500 ETF", 0.85), ("BND", "Vanguard Total Bond Market ETF", 0.15)],
    "hsa": [("FXAIX", "Fidelity 500 Index Fund", 0.65)],
}
# market prices today when no live price is known; ETFs and funds always these (their history is made up)
ANCHORS = {"BTC": 95_000.0, "XAU": 4_000.0, "XAG": 50.0, "XPT": 1_500.0, "HG": 5.0, "SPX": 6_800.0, "IXIC": 23_000.0}
FUND_PRICES = {"VFFVX": 58.0, "VTI": 330.0, "VXUS": 72.0, "VOO": 620.0, "BND": 74.0, "FXAIX": 230.0}
# beta to the market factor, own daily noise, drift a day (log)
FUND_MOVES = {"VFFVX": (0.85, 0.002, 0.0), "VTI": (1.02, 0.002, 0.0), "VXUS": (0.75, 0.006, 0.0001),
              "VOO": (1.0, 0.001, 0.0), "BND": (-0.05, 0.003, 0.00005), "FXAIX": (1.0, 0.001, 0.0)}
HOME, CAR = 300_000.0, 19_000.0
BTC_HELD = {"Hardware wallet": 0.018, "Coinbase": 0.007}
ETH_HELD, ETH_PER_BTC = 0.30, 0.035
GOLD_OZ = 1.0


# ---------- spreadsheet (canned rows the stand-in gws answers with) ----------
def balance_sheet() -> list[list]:
    return [["Assets"], ["Checking", 4000], ["Savings", 9000], ["401(k)", 50000], ["Roth IRA", 14000],
            ["Brokerage", 8500], ["HSA", 4300], ["Total cryptocurrency (USD)", 3000], ["Total metals value", 4000],
            ["Home", HOME], ["Car", CAR], ["Liabilities"], ["Mortgage", 253000], ["Auto loan", 13300],
            ["Visa", 1800], ["Mastercard", 1700], ["Total Equity", 150000]]


INCOME_SHEET = [
    ["Category", "Expense", "Monthly"],
    ["Home", "Mortgage", 1313], ["Home", "Home insurance", 125], ["Home", "House repairs", 60],
    ["Bills", "Electricity", 140], ["Bills", "Water", 55], ["Bills", "Internet", 65], ["Bills", "Cell service", 55],
    ["Car", "Car payment", 418], ["Car", "Car insurance", 110], ["Car", "Gasoline", 170],
    ["Food", "Groceries", 420], ["Food", "Restaurants", 220],
    ["Fun", "Streaming", 30], ["Fun", "Gym", 40], ["Shopping", "Household & Amazon", 150],
    ["Personal", "Haircut", 30], ["Saving", "Roth IRA", 500], ["Saving", "Brokerage", 150], ["", "Total", 4051],
]


def personal_json(start: date) -> dict:
    return {
        "sheet_id": "DEMO-SHEET",
        "networth": {
            "synced": {"Checking": ["Demo Bank Checking%"], "Savings": ["Demo Bank High-Yield Savings%"],
                       "401(k)": ["Demo Employer 401(k)%"], "Roth IRA": ["Demo Roth IRA%"],
                       "Brokerage": ["Demo Individual Brokerage%"], "HSA": ["Demo HSA%"],
                       "Mortgage": ["Demo Home Mortgage%"], "Auto loan": ["Demo Auto Loan%"],
                       "Visa": ["Demo Rewards Visa%"], "Mastercard": ["Demo Cash Back Mastercard%"]},
            "asset_rows": ["Checking", "Savings", "401(k)", "Roth IRA", "Brokerage", "HSA",
                           "Total cryptocurrency (USD)*", "Total metals value*", "Home", "Car"],
            "liab_rows": ["Mortgage", "Auto loan", "Visa", "Mastercard"],
        },
        "rules": [
            [10, "%DEMO CARD CO AUTOPAY%", "checking", "out", "card_payment", "checking side of a card payment"],
            [12, "%TRANSFER TO DEMO BROKERAGE%", "checking", "out", "investment", "Roth IRA and brokerage deposits"],
            [12, "%CONTRIBUTION%", None, "in", "investment", "retirement and brokerage deposits"],
            [12, "%EMPLOYER MATCH%", None, "in", "investment", "401(k) match"],
        ],
        "dashboard": {"ledger_start": start.isoformat()},
        "budget": {"rules": [["DEMO MORTGAGE CO", "mortgage"], ["DEMO HOME INSURANCE", "home insurance"],
                             ["DEMO ELECTRIC", "electricity"], ["DEMO CITY WATER", "water"],
                             ["DEMO FIBER", "internet"], ["DEMO WIRELESS", "cell service"],
                             ["DEMO CREDIT UNION AUTO", "car payment"], ["DEMO AUTO INSURANCE", "car insurance"],
                             ["NETFLIX|HULU|SPOTIFY", "streaming"], ["DEMO FITNESS", "gym"],
                             ["AMAZON|TARGET", "household"]]},
        "subscriptions": {"bill_patterns": ["DEMO ELECTRIC", "DEMO CITY WATER", "DEMO FIBER", "DEMO WIRELESS",
                                            "DEMO HOME INSURANCE", "DEMO AUTO INSURANCE", "DEMO MORTGAGE CO",
                                            "DEMO CREDIT UNION"]},
    }


# ---------- prices ----------
def _walk(rng: random.Random, n: int, anchor: float, sigma: float, drift: float, weekdays_only: bool, today: date,
          factor: list[float] | None = None, beta: float = 0.0) -> dict[date, float]:
    """A price for each of the last n days ending at `anchor` today: log returns drift + beta*factor + noise, walked
    backward. weekdays_only: no move (and no close) on Saturdays and Sundays."""
    out, p = {}, anchor
    for back in range(n):
        d = today - timedelta(days=back)
        if weekdays_only and d.weekday() >= 5:
            continue
        out[d] = round(p, 4)
        r = drift + rng.gauss(0, sigma) + (beta * factor[back] if factor else 0.0)
        p = p / math.exp(r)
    return out


def price_history(today: date, anchors: dict[str, float]) -> tuple[dict, dict]:
    """(prices table: {symbol: {day: usd}}, security_prices: {symbol: {day: usd}})."""
    rng = random.Random(SEED)
    n = 1100
    market = [rng.gauss(0.00045, 0.0095) for _ in range(n)]
    pr = {"SPX": _walk(rng, n, anchors["SPX"], 0.0, 0.0, True, today, market, 1.0),
          "IXIC": _walk(rng, n, anchors["IXIC"], 0.004, 0.0001, True, today, market, 1.2),
          "BTC": _walk(rng, n, anchors["BTC"], 0.027, 0.0012, False, today, market, 0.6),
          "XAU": _walk(rng, n, anchors["XAU"], 0.009, 0.0009, True, today),
          "XAG": _walk(rng, n, anchors["XAG"], 0.016, 0.0009, True, today),
          "XPT": _walk(rng, n, anchors["XPT"], 0.013, 0.0003, True, today),
          "HG": _walk(rng, n, anchors["HG"], 0.012, 0.0002, True, today)}
    sec = {sym: _walk(rng, 400, FUND_PRICES[sym], noise, drift, True, today, market, beta)
           for sym, (beta, noise, drift) in FUND_MOVES.items()}
    return pr, sec


def close_on(series: dict[date, float], d: date) -> float:
    while d not in series:
        d -= timedelta(days=1)
    return series[d]


# ---------- the ledger ----------
def _months_back(today: date, n: int) -> date:
    y, m = today.year, today.month - n
    while m < 1:
        y, m = y - 1, m + 12
    return date(y, m, 1)


class Ledger:
    """Transactions and balances, simulated day by day from DAYS ago to today."""

    def __init__(self, today: date, sec: dict):
        self.today, self.sec = today, sec
        self.start = today - timedelta(days=DAYS - 1)
        self.txns: list[tuple] = []  # (account, day, amount, description, pending)
        self.rng = random.Random(SEED + 1)

    def add(self, aid: str, d: date, amount: float, desc: str, pending: int = 0):
        if self.start <= d <= self.today:
            self.txns.append((aid, d, round(amount, 2), desc, pending))

    def days(self):
        d = self.start
        while d <= self.today:
            yield d
            d += timedelta(days=1)

    def build(self) -> None:
        rng, t = self.rng, self.today
        first_friday = self.start + timedelta(days=(4 - self.start.weekday()) % 7)
        paydays = {first_friday + timedelta(days=14 * k) for k in range(DAYS // 14 + 1)}
        shops = [  # (merchant, card, low, high, chance a day)
            ("KROGER #0417", "visa", 35, 95, 0.14), ("TRADER JOE'S #552", "visa", 22, 68, 0.08),
            ("SHELL OIL 57442", "visa", 34, 52, 0.13), ("STARBUCKS STORE 10921", "visa", 5.4, 8.9, 0.2),
            ("CHIPOTLE 2291", "visa", 11.5, 16.8, 0.1), ("TACO DELI", "mc", 12, 24, 0.08),
            ("DOORDASH*THAI KITCHEN", "visa", 24, 41, 0.06), ("AMAZON.COM*MK2RT", "mc", 12, 85, 0.09),
            ("TARGET 00012", "mc", 18, 74, 0.05), ("HOME DEPOT #6512", "mc", 14, 120, 0.03),
            ("UBER *TRIP", "visa", 11, 29, 0.03), ("CINEMARK THEATRES", "visa", 15, 32, 0.02),
        ]
        for d in self.days():
            if d in paydays:
                self.add("chk", d, NET_PAY, "DEMO EMPLOYER INC PAYROLL")
                self.add("k401", d, K401_PER_CHECK * 0.6, "EMPLOYEE CONTRIBUTION")
                self.add("k401", d, K401_PER_CHECK * 0.4, "EMPLOYER MATCH")
                self.add("hsa", d, HSA_PER_CHECK, "PAYROLL CONTRIBUTION")
            dom = d.day
            if dom == 1:
                self.add("chk", d, -MORTGAGE_PI, "DEMO MORTGAGE CO MTG PAYMENT")
                self.add("mort", d, MORTGAGE_PI, "PAYMENT RECEIVED - THANK YOU")
            if dom == 3:
                self.add("chk", d, -125.0, "DEMO HOME INSURANCE PREMIUM")
            if dom == 5:
                self.add("chk", d, -round(rng.uniform(46, 68), 2), "DEMO CITY WATER UTILITY")
                self.add("chk", d, -500.0, "TRANSFER TO DEMO BROKERAGE ROTH IRA")
                self.add("roth", d, 500.0, "CONTRIBUTION FROM DEMO BANK")
            if dom == 9:
                self.add("chk", d, -round(rng.uniform(96, 178), 2), "DEMO ELECTRIC COOP AUTOPAY")
            if dom == 12:
                self.add("visa", d, -65.0, "DEMO FIBER INTERNET")
                self.add("visa", d, -55.0, "DEMO WIRELESS")
            if dom == 14:
                self.add("visa", d, -15.49, "NETFLIX.COM")
                self.add("visa", d, -11.99, "SPOTIFY USA")
            if dom == 15:
                self.add("chk", d, -AUTO_PAYMENT, "DEMO CREDIT UNION AUTO LOAN PMT")
                self.add("auto", d, AUTO_PAYMENT, "PAYMENT RECEIVED")
                self.add("chk", d, -150.0, "TRANSFER TO DEMO BROKERAGE INDIVIDUAL")
                self.add("brk", d, 150.0, "CONTRIBUTION FROM DEMO BANK")
            if dom == 18:
                self.add("mc", d, -110.0, "DEMO AUTO INSURANCE")
                self.add("visa", d, -40.0, "DEMO FITNESS CLUB")
            if dom == 20:
                self.add("chk", d, -300.0, "TRANSFER TO SAV")
                self.add("sav", d, 300.0, "TRANSFER FROM CHK")
            if dom == 28:
                self.add("sav", d, round(9000 * 0.038 / 12, 2), "INTEREST EARNED")
            if d.month in (2, 5, 8, 11) and dom == 7:
                self.add("brk", d, round(8000 * 0.0035, 2), "DIVIDEND RECEIVED VOO")
            if d.month % 2 == 0 and dom == 21:
                self.add("visa", d, -32.0, "GREAT CLIPS #4410")
            for shop, card, lo, hi, chance in shops:
                if rng.random() < chance:
                    amt = round(rng.uniform(lo, hi), 2)
                    self.add(card, d, -amt, shop, pending=1 if d == t and shop.startswith(("STARBUCKS", "KROGER")) else 0)
            if d == t - timedelta(days=23):
                self.add("mc", d, -349.99, "BEST BUY 00433")  # one bigger buy, so the unusual-charges list has one
        # both cards are paid in full by autopay (the Visa on the 22nd, the Mastercard on the 26th): the statement
        # that closed at the end of the month before
        for d in self.days():
            for card, (day, monthly) in CARD_CYCLE.items():
                if d.day != day:
                    continue
                prev_end = date(d.year, d.month, 1) - timedelta(days=1)
                prev_start = date(prev_end.year, prev_end.month, 1)
                owed = -sum(amt for a, x, amt, _, p in self.txns if a == card and amt < 0 and prev_start <= x <= prev_end)
                # the part of that month from before the ledger starts: a usual month's charges, pro rata
                owed += monthly * max(0, min((self.start - prev_start).days, prev_end.day)) / prev_end.day
                self.add("chk", d, -round(owed, 2), f"DEMO CARD CO AUTOPAY {card.upper()}")
                self.add(card, d, round(owed, 2), "PAYMENT THANK YOU")

    def opening_owed(self, card: str) -> float:
        """What a card owed the day before the ledger starts, as the autopays above assume: the month before's
        statement if its autopay is still to come, plus this month's charges so far (a usual month, pro rata)."""
        day, monthly = CARD_CYCLE[card]
        days_in = (date(self.start.year + self.start.month // 12, self.start.month % 12 + 1, 1) - timedelta(days=1)).day
        return monthly * (self.start.day - 1) / days_in + (monthly if self.start.day <= day else 0.0)

    def balances(self) -> dict[str, dict[date, float]]:
        """Each account's balance at the end of each day: the cash accounts and cards from their transactions
        (ending at today's figure), the loans amortized, the investments as shares x that day's close."""
        out: dict[str, dict[date, float]] = {}
        by_acct: dict[str, dict[date, float]] = {}
        for a, d, amt, _, p in self.txns:
            by_acct.setdefault(a, {}).setdefault(d, 0.0)
            by_acct[a][d] += amt
        target = {a[0]: a[4] for a in ACCOUNTS}
        for aid in ("chk", "sav", "visa", "mc"):
            flows = by_acct.get(aid, {})
            if target[aid] is not None:  # walk back from today's balance
                bal, series = target[aid], {}
                for d in sorted(self.days(), reverse=True):
                    series[d] = round(bal, 2)
                    bal -= flows.get(d, 0.0)
            else:  # cards: forward from the opening balance
                bal, series = -self.opening_owed(aid), {}
                for d in self.days():
                    bal += flows.get(d, 0.0)
                    series[d] = round(bal, 2)
            out[aid] = series
        for aid, rate in (("mort", MORTGAGE_RATE), ("auto", AUTO_RATE)):
            pays = by_acct.get(aid, {})
            bal, series = target[aid], {}
            for d in sorted(self.days(), reverse=True):
                series[d] = round(bal, 2)
                if pays.get(d):  # before this payment: interest for the month, then the payment came off
                    bal = (bal - pays[d]) / (1 + rate / 12)
            out[aid] = series
        self.holdings: dict[str, dict[date, list]] = {}
        for aid, funds in FUNDS.items():
            flows = by_acct.get(aid, {})
            today_val = target[aid]
            # today's shares of each fund; going back, each deposit's dollars come off in the same split
            shares = {sym: today_val * w / close_on(self.sec[sym], self.today) for sym, _, w in funds}
            cash = today_val * (1 - sum(w for _, _, w in funds))
            series, held = {}, {}
            for d in sorted(self.days(), reverse=True):
                pos = [(sym, desc, shares[sym], close_on(self.sec[sym], d)) for sym, desc, _ in funds]
                series[d] = round(cash + sum(s * px for _, _, s, px in pos), 2)
                held[d] = [(sym, desc, round(s, 4), px) for sym, desc, s, px in pos]
                dep = flows.get(d, 0.0)
                if dep:
                    for sym, _, w in funds:
                        shares[sym] -= dep * w / close_on(self.sec[sym], d)
                    cash -= dep * (1 - sum(w for _, _, w in funds))
            out[aid] = series
            self.holdings[aid] = held
        return out


def build_ledger(path: str, today: date, anchors: dict[str, float]) -> dict:
    """Write the demo ledger; returns what the parent needs to know (today's crypto and metals values). Run it with
    WEALTH_LEDGER_PERSONAL pointing at the demo's personal.json (ctdb seeds its rules from it)."""
    sys.path.insert(0, str(ROOT))
    import ctdb
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    prices, sec = price_history(today, anchors)
    led = Ledger(today, sec)
    led.build()
    bals = led.balances()
    db = ctdb.connect(path)
    names = {a[0]: (a[1], a[2]) for a in ACCOUNTS}
    for aid, name, inst, kind, _ in ACCOUNTS:
        db.execute("INSERT OR REPLACE INTO account_kind VALUES (?,?,NULL)", (aid, kind))
        for d, bal in bals[aid].items():
            iso = d.isoformat()
            if aid in CARD_LIMITS:
                avail = max(CARD_LIMITS[aid] + bal, 0)
                db.execute(ctdb.UPSERT_BALANCE_LIMIT, (iso, "simplefin", aid, name, inst, f"{bal:.2f}", f"{avail:.2f}",
                                                       "USD", iso, str(CARD_LIMITS[aid])))
            else:
                db.execute(ctdb.UPSERT_BALANCE, (iso, "simplefin", aid, name, inst, f"{bal:.2f}", None, "USD", iso))
        for d, pos in getattr(led, "holdings", {}).get(aid, {}).items():
            for sym, desc, shares, px in pos:
                db.execute(ctdb.UPSERT_HOLDING, (d.isoformat(), "simplefin", aid, name, inst, sym, sym, desc,
                                                 str(shares), str(px), f"{shares * px:.2f}", None, "USD"))
    for n, (aid, d, amt, desc, pending) in enumerate(sorted(led.txns, key=lambda x: (x[1], x[0], x[3]))):
        db.execute(ctdb.upsert_sql("txns"), (f"demo-{n}", aid, *names[aid], d.isoformat(), None, f"{amt:.2f}", desc,
                                             None, None, pending, "USD", now, now, "simplefin"))
    for sym, series in prices.items():
        db.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?,?)",
                       [(d.isoformat(), sym, px, "demo") for d, px in series.items()])
    for sym, series in sec.items():
        db.executemany("INSERT OR REPLACE INTO security_prices VALUES (?,?,?,?)",
                       [(d.isoformat(), sym, px, "demo") for d, px in series.items()])
    db.execute("INSERT OR REPLACE INTO meta VALUES ('prices_updated', ?)", (now,))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('inflation', ?)", (json.dumps({"pct": 2.9, "asof": today.isoformat()}),))
    btc = close_on(prices["BTC"], today)
    eth = round(btc * ETH_PER_BTC, 2)
    gold = close_on(prices["XAU"], today)
    holdings = [{"wallet": w, "coin": "BTC", "qty": q, "price": btc, "value": round(q * btc, 2),
                 "qty_source": "onchain" if w == "Hardware wallet" else "exchange-api", "counted": True}
                for w, q in BTC_HELD.items()]
    holdings.append({"wallet": "Coinbase", "coin": "ETH", "qty": ETH_HELD, "price": eth, "value": round(ETH_HELD * eth, 2),
                     "qty_source": "exchange-api", "counted": True})
    btc_qty = sum(BTC_HELD.values())
    crypto = {"holdings": holdings, "sheet_total": None, "warnings": [],
              "by_coin": {"BTC": {"qty": btc_qty, "price": btc, "value": round(btc_qty * btc, 2)},
                          "ETH": {"qty": ETH_HELD, "price": eth, "value": round(ETH_HELD * eth, 2)}},
              "prices": {"BTC": {"usd": btc, "source": "demo"}}}
    crypto_value = round(sum(h["value"] for h in holdings), 2)
    metals = {"metals": {"gold": {"spot": gold, "oz": GOLD_OZ, "source": "demo"}}}
    for asset, value, source, detail in (
            ("Total cryptocurrency (USD)", crypto_value, "qty: onchain/exchange-api", crypto),
            ("Total metals value", round(GOLD_OZ * gold, 2), "spot x ounces", metals),
            ("Home", HOME, "demo estimate", None)):
        db.execute("INSERT OR REPLACE INTO valuations VALUES (?,?,?,?,?,?)",
                   (today.isoformat(), asset, value, source, json.dumps(detail) if detail else None, now))
    # whole net worth from the "yearly spreadsheets", every 4 weeks for three years before the daily snapshots
    first = led.start
    rng = random.Random(SEED + 2)
    for k in range(1, 40):
        d = first - timedelta(days=28 * k)
        nw = 145_000 - 1_450 * k + rng.uniform(-2_500, 2_500)
        db.execute("INSERT OR REPLACE INTO networth_history VALUES (?,?,?,?,?,?)",
                   (d.isoformat(), round(nw, 2), None, None, "demo", now))
    for k, (direction, amount, memo) in enumerate((("in", 0.006, "from Coinbase"), ("in", 0.004, "from Coinbase"),
                                                    ("out", 0.001, None))):
        d = today - timedelta(days=12 + 31 * k)
        db.execute("INSERT OR REPLACE INTO crypto_transfers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   ("Hardware wallet", "BTC", f"demo-tx-{k}", direction, d.isoformat(), amount, 0.000012,
                    "bc1q-demo-address", None, memo, "demo"))
    db.commit()
    db.close()
    return {"btc": btc, "gold": gold}


# ---------- building the answers in a sealed child process ----------
FAKE_GWS = '''#!{python}
"""Stand-in for the gws CLI in the demo: answers a spreadsheet read from canned rows. Calls nothing."""
import json, sys
args = sys.argv[1:]
try:
    rng = json.loads(args[args.index("--params") + 1])["range"]
    tabs = json.load(open({sheets!r}))
except (ValueError, KeyError, IndexError, OSError) as e:
    sys.exit(f"demo gws: unexpected call: {{e!r}}")
tab = rng.split("!")[0]
if tab not in tabs:
    sys.exit(f"demo gws: no rows for {{rng}}")
print(json.dumps({{"range": rng, "values": tabs[tab]}}))
'''


def sealed_env(folder: Path) -> dict[str, str]:
    """The child's whole environment, made from scratch: nothing of the server's own is passed on."""
    d = str(folder)
    env = {"HOME": f"{d}/home", "PATH": f"{d}/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
           "WEALTH_LEDGER_CONFIG_DIR": f"{d}/conf", "WEALTH_LEDGER_DB": f"{d}/ledger.db",
           "WEALTH_LEDGER_STATUS": f"{d}/status", "WEALTH_LEDGER_PERSONAL": f"{d}/conf/personal.json",
           "WEALTH_LEDGER_ACCESS": f"{d}/conf/no-simplefin", "WEALTH_LEDGER_LOCK": f"{d}/sync.lock",
           "WEALTH_LEDGER_SYNC_LOG": f"{d}/sync.log", "WEALTH_LEDGER_SYNC_CMD": "/bin/false",
           "WEALTH_LEDGER_EMAIL_STATE": f"{d}/email-state.json", "PLAID_CONFIG": f"{d}/plaid.json",
           "WEALTH_LEDGER_LIVE_MARKETS": "off", "WEALTH_LEDGER_PYTHON": sys.executable}
    if os.environ.get("TZ"):
        env["TZ"] = os.environ["TZ"]
    return env


def _dump(folder: Path, today: date, anchors: dict[str, float]) -> None:
    """In the sealed child: write the ledger, then print every route's answer as one JSON object."""
    import io
    import contextlib
    info = build_ledger(str(folder / "ledger.db"), today, anchors)
    sys.path.insert(0, str(HERE))
    with contextlib.redirect_stdout(io.StringIO()):
        import server
    out: dict[str, object] = {}
    for route, fn in server.ROUTES.items():
        if route == "/api/account_txns":
            continue
        out[route.removeprefix("/api/")] = fn({})
    out["account_txns"] = {aid: server.api_account_txns({"id": [aid]}) for aid, *_ in ACCOUNTS}
    out["_info"] = info
    sys.stdout.write(json.dumps(out))


def build(today: date | None = None, anchors: dict[str, float] | None = None) -> dict:
    """Every demo answer: {route name: JSON bytes, "account_txns": {account id: JSON bytes}}. Raises on failure."""
    today = today or date.today()
    anchors = {**ANCHORS, **{k: float(v) for k, v in (anchors or {}).items() if k in ANCHORS and v and v > 0}}
    folder = Path(tempfile.mkdtemp(prefix="wealth-ledger-demo-"))
    try:
        for p in ("conf", "bin", "home"):
            (folder / p).mkdir()
        start = _months_back(today, 3)
        if (today - start).days >= DAYS:
            start = today - timedelta(days=DAYS - 1)
        (folder / "conf" / "personal.json").write_text(json.dumps(personal_json(start)))
        (folder / "sheets.json").write_text(json.dumps({"balance sheet": balance_sheet(), "income sheet": INCOME_SHEET}))
        gws = folder / "bin" / "gws"
        gws.write_text(FAKE_GWS.format(python=sys.executable, sheets=str(folder / "sheets.json")))
        gws.chmod(gws.stat().st_mode | stat.S_IXUSR)
        ran = datetime.combine(today, datetime.min.time()).replace(hour=6).astimezone()
        if ran > datetime.now().astimezone():
            ran = datetime.now().astimezone()
        (folder / "status").write_text(f"last run {ran.isoformat(timespec='seconds')} rc=0\n")
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--dump", str(folder), today.isoformat(),
                            json.dumps(anchors)], env=sealed_env(folder), cwd=str(folder), capture_output=True,
                           timeout=BUILD_TIMEOUT_S)
        if r.returncode:
            raise RuntimeError(f"demo build failed: {r.stderr.decode(errors='replace')[-2000:]}")
        # a note may name the demo's own settings file (the cards a person has no settings for say where to add them):
        # the temp folder means nothing to a visitor
        text = r.stdout.decode().replace(json.dumps(str(folder / "conf" / "personal.json"))[1:-1], "personal.json")
        if str(folder) in text:
            raise RuntimeError("demo build: an answer names the build folder")
        raw = json.loads(text)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    info = raw.pop("_info", {})
    out = {k: json.dumps(v).encode() for k, v in raw.items() if k != "account_txns"}
    out["account_txns"] = {k: json.dumps(v).encode() for k, v in raw["account_txns"].items()}
    out["_info"] = info
    out["_summary"] = raw.get("summary") or {}
    return out


class Demo:
    """The demo's answers, built once a day (on first use, or by warm()), kept in memory. While a new day's copy is
    built, the previous day's keeps being served; only the very first build makes a request wait."""

    def __init__(self, anchors=lambda: {}):
        self._anchors = anchors
        self._lock = threading.Lock()
        self._data: dict | None = None
        self._day: str | None = None
        self._building = False
        self.error: str | None = None
        self.failed_at = 0.0

    def _build(self) -> None:
        day = date.today().isoformat()
        try:
            data = build(date.fromisoformat(day), self._anchors())
            with self._lock:
                self._data, self._day, self.error = data, day, None
        except Exception as e:  # noqa: BLE001 - reported to the page as "demo unavailable"
            self.error, self.failed_at = repr(e)[-500:], time.monotonic()
            print(f"demo: {self.error}", file=sys.stderr, flush=True)
        finally:
            self._building = False

    def warm(self) -> None:
        """Start building today's copy in the background if there is none and none is under way."""
        with self._lock:
            stale = self._day != date.today().isoformat()
            retry_ok = not self.error or time.monotonic() - self.failed_at > 300
            if not stale or self._building or not retry_ok:
                return
            self._building = True
        threading.Thread(target=self._build, daemon=True).start()

    def get(self, wait: float = 60.0) -> dict | None:
        """Today's answers (yesterday's while today's builds), or None when there are none yet after `wait`."""
        self.warm()
        give_up = time.monotonic() + wait
        while self._data is None and (self._building or not self.error) and time.monotonic() < give_up:
            time.sleep(0.05)
            self.warm()
        return self._data


def main(argv: list[str]) -> int:
    if argv[:1] == ["--dump"]:
        folder, today, anchors = Path(argv[1]), date.fromisoformat(argv[2]), json.loads(argv[3])
        _dump(folder, today, anchors)
        return 0
    if argv[:1] == ["--summary"]:
        t0 = time.time()
        d = build()
        s = d["_summary"]
        print(f"built in {time.time() - t0:.1f}s")
        print(f"net worth {s.get('net_worth'):,.0f}  assets {s.get('assets'):,.0f}  liabilities {s.get('liabilities'):,.0f}")
        for c in s.get("categories") or []:
            if c["value"]:
                print(f"  {c['label']:<28}{c['value']:>12,.0f}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
