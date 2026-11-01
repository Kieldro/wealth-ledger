"""Zelle memos from bank emails, against a synthetic ledger and made-up email text (no real data)."""
from datetime import date

import pytest

import ctdb
from insights import zelle_memos as zm

SENT = ("Dear Pat, We're confirming that your Zelle payment was successfully credited to Sam. Payment Details "
        "Confirmation Number 123 Amount $1,250.00 From Example Checking - ******0000 To Sam Rivera "
        "(sam@example.com) Message march rent refund Thank you for using Example Bank and Zelle.")
RECEIVED = ('ALEX MORGAN sent you $75.50 "utilities share" Dear Pat, Your payment from ALEX will be deposited '
            "into your bank account on or about 03/04/2026. Deposit in Progress Amount $75.50 From ALEX MORGAN "
            "Delivery On or About 03/04/2026 The actual deposit date could vary.")


@pytest.fixture
def con(tmp_path):
    db = ctdb.connect(tmp_path / "t.db")
    db.execute("INSERT INTO account_kind VALUES ('chk','checking',NULL)")
    return db


def txn(con, tid, d, amount, desc, memo=None):
    con.execute(ctdb.upsert_sql("txns"), (tid, "chk", "Checking", "Bank", d, None, f"{amount:.2f}", desc, None, memo,
                                          0, "USD", "x", "x", "test"))


def test_parse_sent_and_received():
    assert zm.parse(SENT, date(2026, 3, 2)) == {"direction": "out", "amount": 1250.0, "name": "Sam Rivera",
                                                "memo": "march rent refund", "date": date(2026, 3, 2)}
    r = zm.parse(RECEIVED, date(2026, 3, 1))
    assert (r["direction"], r["amount"], r["name"], r["memo"], r["date"]) == (
        "in", 75.5, "ALEX MORGAN", "utilities share", date(2026, 3, 4))
    assert zm.parse("Your statement is ready", date(2026, 3, 1)) is None


def test_memo_lands_on_the_right_zelle_and_survives_a_resync(con):
    txn(con, "a", "2026-03-03", -1250, "Zelle payment to SAM  RIVERA")
    txn(con, "b", "2026-03-03", -1250, "Zelle payment to OTHER PERSON")
    txn(con, "c", "2026-03-04", 75.5, "ALEX MORGAN XYZ ZELLE")
    done = zm.apply(con, [zm.parse(SENT, date(2026, 3, 2)), zm.parse(RECEIVED, date(2026, 3, 1))])
    assert sorted(t for t, _ in done) == ["a", "c"]
    txn(con, "a", "2026-03-03", -1250, "Zelle payment to SAM  RIVERA", memo="")  # feed re-sends it with no memo
    memos = dict(con.execute("SELECT txn_id, memo FROM txns"))
    assert memos == {"a": "march rent refund", "b": None, "c": "utilities share"}


def test_contact_name_differs_from_legal_name_only_if_unambiguous(con):
    note = zm.parse(SENT.replace("Sam Rivera", "Sam Cleaning"), date(2026, 3, 2))
    txn(con, "a", "2026-03-03", -1250, "Zelle payment to SAMANTHA  RIVERA LOPEZ")
    assert [t for t, _ in zm.apply(con, [note], dry_run=True)] == ["a"]
    txn(con, "b", "2026-03-04", -1250, "Zelle payment to SOMEONE  ELSE")
    assert zm.apply(con, [note], dry_run=True) == []  # two candidates, no name match: leave both alone


def test_outside_the_date_window_or_already_noted_is_skipped(con):
    txn(con, "a", "2026-03-20", -1250, "Zelle payment to SAM  RIVERA")
    txn(con, "b", "2026-03-03", -1250, "Zelle payment to SAM  RIVERA", memo="typed by me")
    assert zm.apply(con, [zm.parse(SENT, date(2026, 3, 2))]) == []
    assert dict(con.execute("SELECT txn_id, memo FROM txns"))["b"] == "typed by me"
