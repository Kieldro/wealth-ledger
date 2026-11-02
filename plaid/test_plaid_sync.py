"""Plaid balance snapshots and card credit limits, against a synthetic DB and a stubbed API (no network)."""
import sqlite3

import pytest

import ctdb
from plaid import plaid_sync

ACCOUNTS = [  # Plaid shape: owed amounts positive; limit only for cards that report one
    {"account_id": "card1", "name": "Card", "official_name": "Test Rewards Card", "mask": "1111", "type": "credit",
     "balances": {"current": 300.0, "available": 1700.0, "limit": 2000.0, "iso_currency_code": "USD"}},
    {"account_id": "card2", "name": "Example Rewards Card", "mask": "2222", "type": "credit",
     "balances": {"current": 50.0, "available": None, "limit": None, "iso_currency_code": "USD"}},
    {"account_id": "chk", "name": "Checking", "mask": "3333", "type": "depository",
     "balances": {"current": 900.0, "available": 900.0, "limit": None, "iso_currency_code": "USD"}},
]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(plaid_sync, "call", lambda cfg, path, body: {"accounts": ACCOUNTS})
    con = ctdb.connect(tmp_path / "t.db")
    yield con
    con.close()


def accounts(con):
    con.row_factory = sqlite3.Row
    return {r["account_id"]: dict(r) for r in con.execute("SELECT * FROM v_accounts")}


def test_balances_store_credit_limit(db):
    plaid_sync.account_names({}, {"access_token": "x", "institution": "Bank"}, db)
    a = accounts(db)
    assert a["card1"]["balance"] == -300.0
    assert (a["card1"]["credit_limit"], a["card1"]["credit_limit_source"]) == (2000.0, "plaid")
    # no feed limit: falls back to personal.example.json credit_limits ("Example Rewards Card%": 5000)
    assert (a["card2"]["credit_limit"], a["card2"]["credit_limit_source"]) == (5000.0, "manual")
    assert a["chk"]["credit_limit"] is None and a["chk"]["credit_limit_source"] is None


def test_compare_items_write_no_balances(db):
    plaid_sync.account_names({}, {"access_token": "x", "mode": "compare"}, db)
    assert db.execute("SELECT COUNT(*) FROM balances").fetchone()[0] == 0


def test_feed_limit_beats_manual_and_zero_is_unknown(db):
    db.execute(ctdb.UPSERT_BALANCE_LIMIT, ("2026-01-02", "plaid", "c9", "Example Rewards Card (9)", "B", "-1", None,
                                           "USD", None, "8000.00"))
    db.execute(ctdb.UPSERT_BALANCE_LIMIT, ("2026-01-02", "plaid", "c0", "Zero Card (0)", "B", "-1", None, "USD", None,
                                           "0.00"))
    a = accounts(db)
    assert (a["c9"]["credit_limit"], a["c9"]["credit_limit_source"]) == (8000.0, "plaid")
    assert a["c0"]["credit_limit"] is None


def test_connect_migrates_old_balances_table(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE balances (snapshot_date TEXT NOT NULL, source TEXT NOT NULL, account_id TEXT NOT NULL, "
                "account_name TEXT, institution TEXT, balance TEXT, available TEXT, currency TEXT, as_of TEXT, "
                "PRIMARY KEY (snapshot_date, source, account_id))")
    old.execute("INSERT INTO balances VALUES ('2026-01-01','simplefin','a','A (1)','B','-5',NULL,'USD',NULL)")
    old.commit(); old.close()
    con = ctdb.connect(path)
    assert "credit_limit" in {r[1] for r in con.execute("PRAGMA table_info(balances)")}
    assert con.execute("SELECT balance, credit_limit FROM v_accounts").fetchone() == (-5.0, None)
    con.close()


def test_bad_manual_limit_is_rejected(monkeypatch):
    monkeypatch.setattr(ctdb.personal, "load", lambda: {"credit_limits": {"_help": "x", "Card%": "lots"}})
    with pytest.raises(ctdb.personal.PersonalConfigError):
        ctdb.personal_credit_limits()
