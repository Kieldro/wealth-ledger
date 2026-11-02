"""Overdraft forecast against a small synthetic ledger (no real data)."""
from datetime import date, timedelta

import pytest

import ctdb
from insights import overdraft

TODAY = date(2026, 10, 27)
CHK, CARD, IDLE = "chk", "card", "idle"


class Ledger:
    def __init__(self, path):
        self.db = ctdb.connect(path)
        self.n = 0

    def account(self, aid, name, kind, balance):
        self.db.execute("INSERT INTO account_kind VALUES (?,?,NULL)", (aid, kind))
        self.db.execute(ctdb.UPSERT_BALANCE, (TODAY.isoformat(), "test", aid, name, "Bank", str(balance), None, "USD",
                                              TODAY.isoformat()))

    def txn(self, aid, d, amount, desc):
        self.n += 1
        self.db.execute(ctdb.upsert_sql("txns"), (f"t{self.n}", aid, aid, "Bank", d.isoformat(), None, f"{amount:.2f}",
                                                  desc, None, None, 0, "USD", "x", "x", "test"))

    def monthly(self, aid, first, months, amount, desc, every=None):
        for i in range(months):
            d = first + timedelta(days=every * i) if every else first.replace(month=first.month + i)
            self.txn(aid, d, amount, desc)

    def forecast(self):
        self.db.commit()
        return {a["account_id"]: a for a in overdraft.forecast(self.db, today=TODAY)}


@pytest.fixture
def led(tmp_path):
    return Ledger(tmp_path / "t.db")


def test_rent_overdraws_and_warns_with_date(led):
    led.account(CHK, "Checking (1111)", "checking", 1500)
    led.monthly(CHK, date(2026, 7, 3), 4, -2100, "DIRECT DEBIT LANDLORD RENT (Cash)")
    led.monthly(CHK, date(2026, 7, 13), 4, -90, "DEBIT CARD PURCHASE PHONE CO 071226 AUTHID:123")
    fc = led.forecast()[CHK]
    rent = [e for e in fc["events"] if "LANDLORD" in e["label"]]
    assert [e["date"] for e in rent] == ["2026-11-03"] and rent[0]["amount"] == -2100
    assert fc["first_short"] == "2026-11-03" and fc["low"] == pytest.approx(1500 - 2100 - 90)
    (w,) = overdraft.warnings(list(led.forecast().values()))
    assert w.startswith("Overdraft risk: Checking (1111) has $1,500, projected -$690")
    assert "Move money in before 11/3" in w


def test_payroll_needs_three_and_ignores_bonus(led):
    led.account(CHK, "Checking", "checking", 5000)
    for d in (date(2026, 7, 30), date(2026, 8, 31), date(2026, 9, 29)):
        led.txn(CHK, d, 3750, "DIRECT DEPOSIT ACME PAYROLL")
    led.txn(CHK, date(2026, 8, 24), 2566, "DIRECT DEPOSIT ACME PAYROLL")  # one-off: must not join the series
    for d in (date(2026, 9, 14), date(2026, 10, 14)):  # only two: not trusted yet
        led.txn(CHK, d, 3750, "DIRECT DEPOSIT ACME PAYROLL")
    ev = led.forecast()[CHK]["events"]
    assert [(e["date"], e["amount"]) for e in ev] == [("2026-10-29", 3750)]


def test_late_draft_counts_as_due_today_and_stopped_series_drops(led):
    led.account(CHK, "Checking", "checking", 1000)
    led.monthly(CHK, date(2026, 7, 25), 3, -100, "DIRECT DEBIT GYM")      # next 10/25: 2 days late -> due today
    led.monthly(CHK, date(2026, 5, 5), 3, -100, "DIRECT DEBIT OLD SUB")   # last 7/5: stopped
    ev = led.forecast()[CHK]["events"]
    assert [(e["label"], e["date"]) for e in ev][:1] == [("GYM", TODAY.isoformat())]
    assert not any("OLD SUB" in e["label"] for e in ev)


def test_bill_that_skipped_a_whole_cycle_drops(led):
    led.account(CHK, "Checking", "checking", 1000)
    led.monthly(CHK, date(2026, 7, 12), 3, -2100, "DIRECT DEBIT LANDLORD RENT")  # 9/12, then no 10/12 debit
    led.monthly(CHK, date(2026, 7, 20), 3, -60, "DIRECT DEBIT POWER CO")       # 9/20 + 31 days: 10/21, late but still due
    ev = led.forecast()[CHK]["events"]
    assert not any("LANDLORD" in e["label"] for e in ev)
    assert [e["date"] for e in ev if "POWER" in e["label"]] == ["2026-11-21"]


def test_sweeps_and_idle_accounts_ignored(led):
    led.account(CHK, "Checking", "checking", 800)
    led.account(IDLE, "Biz checking", "checking", 10)
    led.monthly(CHK, date(2026, 7, 10), 4, 500, "REDEMPTION FROM CORE ACCOUNT FDIC")
    led.monthly(CHK, date(2026, 7, 10), 4, -500, "PURCHASE INTO CORE ACCOUNT FDIC")
    fc = led.forecast()
    assert fc[CHK]["events"] == [] and overdraft.warnings(list(fc.values())) == []


def test_card_autopay_sized_from_learned_statement_close(led):
    """Charges on the 1st and 15th, autopay at month end for what was owed mid-month. The next autopay (10/29)
    must be the $900 owed at that close, not the $1,400 owed today."""
    led.account(CHK, "Checking", "checking", 3000)
    led.account(CARD, "Visa (2222)", "credit", -1400)
    led.db.execute("INSERT INTO txn_rules VALUES (10, '%EPAYMENT%', 'checking', 'out', 'card_payment', NULL)")
    for m in (7, 8, 9, 10):
        led.txn(CARD, date(2026, m, 1), -400, "STORE A")
        led.txn(CARD, date(2026, m, 15), -500, "STORE B")
    for d, amt in ((date(2026, 7, 30), 400), (date(2026, 8, 30), 900), (date(2026, 9, 29), 900)):
        led.txn(CARD, d, amt, "AUTOPAY PAYMENT - THANK YOU")
        led.txn(CHK, d + timedelta(days=1), -amt, "DIRECT DEBIT VISA EPAYMENT")
    ev = led.forecast()[CHK]["events"]
    assert [(e["date"], e["label"], e["amount"]) for e in ev] == [("2026-10-29", "Visa (2222) autopay", -900)]
