"""Monthly summary email against a synthetic ledger (no real data, no sheet); nothing is ever sent."""
import base64
import email
import json
from datetime import date

import pytest

import ctdb
from insights import monthly_email as me

TODAY = date(2026, 11, 3)


class Ledger:
    def __init__(self, path):
        self.db = ctdb.connect(path)
        self.n = 0
        for aid, kind in (("chk", "checking"), ("card", "credit")):
            self.db.execute("INSERT INTO account_kind VALUES (?,?,NULL)", (aid, kind))

    def txn(self, d, amount, desc, payee=None, aid="card", pending=0):
        self.n += 1
        self.db.execute(ctdb.upsert_sql("txns"), (f"t{self.n}", aid, aid, "Bank", d.isoformat(), None,
                                                  f"{amount:.2f}", desc, payee, None, pending, "USD", "x", "x",
                                                  "test"))

    def snapshot(self, aid, d, balance):
        self.db.execute(ctdb.UPSERT_BALANCE, (d.isoformat(), "test", aid, aid, "Bank", str(balance), None, "USD",
                                              d.isoformat()))

    def report(self, **kw):
        self.db.commit()
        return me.report(self.db, "2026-10", today=TODAY, **kw)


@pytest.fixture
def led(tmp_path):
    led = Ledger(tmp_path / "t.db")
    led.txn(date(2026, 10, 2), 3000, "DIRECT DEPOSIT EXAMPLE PAYROLL", aid="chk")
    led.txn(date(2026, 10, 5), -450, "H-E-B #123")
    led.txn(date(2026, 10, 9), -300, "H-E-B #123")
    led.txn(date(2026, 10, 12), -40, "SHELL OIL 5551")
    led.txn(date(2026, 10, 20), -75, "CORNER BOOKSHOP")
    led.txn(date(2026, 10, 21), -99, "H-E-B #123", pending=1)   # pending: not counted
    led.txn(date(2026, 11, 1), -500, "H-E-B #123")              # next month
    led.txn(date(2026, 9, 8), -200, "H-E-B #123")               # the month before
    led.txn(date(2026, 9, 9), -60, "CITY ELECTRIC CO")
    return led


def cats(rep):
    return {c["category"]: (c["spent"], c["prev"]) for c in rep["categories"]}


def test_report_totals_categories_and_rankings(led):
    rep = led.report()
    assert (rep["income"], rep["spent"], rep["net"], rep["prev_spent"]) == (3000, 865, 2135, 260)
    assert cats(rep) == {"Groceries": (750, 200), "Other": (75, 0), "Utilities & phone": (0, 60),
                         "Car & transport": (40, 0)}
    assert [c["category"] for c in rep["categories"]][0] == "Groceries" and rep["prev_label"] == "September"
    assert rep["top_merchants"][0] == {"merchant": "H-E-B", "spent": 750, "n": 2}
    assert [c["amount"] for c in rep["largest"]] == [450, 300, 75, 40]
    assert rep["rental"] is None and rep["balances"] is None and rep["label"] == "October 2026"


def test_own_category_rules_win_and_an_incomplete_month_before_is_not_compared(led):
    rep = led.report(categories=[["BOOKSHOP", "Books"], ["H-E-B", "Food"]], since="2026-09-15")
    assert cats(rep) == {"Food": (750, None), "Books": (75, None), "Car & transport": (40, None)}
    assert rep["prev_spent"] is None and rep["prev_label"] is None and not rep["partial"]
    assert "September" not in me.render_text(rep)
    assert led.report(since="2026-10-10")["partial"]


def test_generic_categories():
    rl = me.category_rules()
    for desc, want in (("COSTCO GAS #12", "Car & transport"), ("COSTCO WHSE #12", "Groceries"),
                       ("UBER EATS", "Dining"), ("UBER TRIP", "Car & transport"), ("AMAZON PRIME*X", "Subscriptions"),
                       ("AMAZON MKTPL*X", "Shopping"), ("EXAMPLE RENT A CAR", "Travel"), ("MONTHLY FEE", "Fees & interest"),
                       ("Zelle payment to A PERSON", "People (Zelle, Venmo)"), ("SOMETHING ELSE", "Other")):
        assert me.categorize(desc, None, rl) == want, desc


def test_balance_change_compares_like_with_like(led):
    led.snapshot("chk", date(2026, 9, 30), 5000)
    led.snapshot("chk", date(2026, 10, 31), 6500)
    led.snapshot("card", date(2026, 10, 10), -900)   # linked during the month: its balance is not a loss
    led.snapshot("chk", date(2026, 11, 2), 9999)     # after the month
    assert led.report()["balances"] == {"start": 5000, "end": 6500, "delta": 1500, "accounts": 1, "new_accounts": 1,
                                        "since": None}


def test_balance_change_when_snapshots_begin_inside_the_month(led):
    led.snapshot("chk", date(2026, 10, 16), 5000)
    led.snapshot("chk", date(2026, 10, 31), 5400)
    b = led.report()["balances"]
    assert (b["delta"], b["since"]) == (400, "2026-10-16")
    led.db.execute("DELETE FROM balances WHERE snapshot_date='2026-10-31'")
    assert led.report()["balances"] is None           # one day of snapshots: nothing to compare


def test_bodies_hold_the_same_sections_and_escape_html(led):
    led.txn(date(2026, 10, 22), -20, "<B>ODD & SONS")
    rep = led.report(problems=["Plaid sync failed"])
    text, html = me.render_text(rep), me.render_html(rep)
    for want in ("Spent in September", "Plaid sync failed", "+$550", "Groceries"):
        assert want in text and want in html
    assert "SPENDING BY CATEGORY" in text and "Spending by category" in html
    assert "<B>" not in html and "&lt;B&gt;Odd &amp; Sons" in html
    assert me.subject(rep) == "Wealth Ledger: October 2026: spent $885, net +$2,115"


def test_send_goes_to_the_signed_in_address_unless_told(led):
    calls = []

    def gws(*args):
        calls.append(args)
        return {"emailAddress": "owner@example.com"} if "getProfile" in args else {"id": "m1"}
    assert me.send(led.report(), gws=gws) == "m1"
    raw = json.loads(calls[-1][-1])["raw"]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert msg["To"] == "owner@example.com" and msg["Subject"].startswith("Wealth Ledger: October 2026")
    assert {p.get_content_type() for p in msg.walk()} >= {"text/plain", "text/html"}
    calls.clear()
    me.send(led.report(), to="other@example.com", gws=gws)
    assert len(calls) == 1                             # no profile lookup


def test_due_once_a_month_after_the_send_day_and_only_when_enabled(tmp_path):
    state = tmp_path / "sent"
    on = {"enabled": True}
    assert me.due(date(2026, 11, 2), on, state) is None            # before day 3
    assert me.due(date(2026, 11, 3), on, state) == "2026-10"
    assert me.due(date(2026, 11, 20), on, state) == "2026-10"      # a missed day catches up
    assert me.due(date(2026, 11, 1), {"enabled": True, "day": 1}, state) == "2026-10"
    assert me.due(date(2026, 11, 3), {}, state) is None            # off by default
    state.write_text("2026-10\n")
    assert me.due(date(2026, 11, 20), on, state) is None
    assert me.due(date(2026, 12, 3), on, state) == "2026-11"


def test_printing_never_sends(led, monkeypatch, capsys, tmp_path):
    led.db.commit()
    monkeypatch.setattr(me.subprocess, "run", lambda *a, **k: pytest.fail("ran a command"))   # no gws, no sheet
    monkeypatch.setattr(me, "send", lambda *a, **k: pytest.fail("sent"))
    monkeypatch.setattr(me, "STATE", tmp_path / "sent")
    assert me.main(["--month", "2026-10", "--db", str(tmp_path / "t.db")]) == 0
    assert "Subject: Wealth Ledger: October 2026" in capsys.readouterr().out
    assert me.main(["--if-due", "--db", str(tmp_path / "t.db")]) == 0     # example config: not enabled
    assert "not due" in capsys.readouterr().out and not (tmp_path / "sent").exists()
