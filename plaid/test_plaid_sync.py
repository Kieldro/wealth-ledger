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


def _two_items(monkeypatch, tmp_path, fail=("tokA",)):
    """Two linked banks against a stubbed API: /accounts/get always answers from Plaid's cache, /transactions/sync
    fails for the access tokens in `fail` (what an item that needs a new login does)."""
    def call(cfg, path, body):
        tok = body["access_token"]
        if path == "/accounts/get":
            return {"accounts": [dict(a, account_id=f"{tok}-{a['account_id']}") for a in ACCOUNTS]}
        if tok in fail:
            raise plaid_sync.PlaidError(f"{path}: ITEM_LOGIN_REQUIRED: the login details of this item have changed")
        return {"added": [], "modified": [], "removed": [], "next_cursor": "c1", "has_more": False}
    monkeypatch.setattr(plaid_sync, "call", call)
    monkeypatch.setattr(plaid_sync, "save_config", lambda cfg: None)  # never touch a real plaid.json
    cfg = {"items": [{"item_id": "a", "access_token": "tokA", "institution": "Bank A"},
                     {"item_id": "b", "access_token": "tokB", "institution": "Bank B"}]}
    monkeypatch.setattr(plaid_sync, "load_config", lambda: cfg)
    path = tmp_path / "t.db"
    con = ctdb.connect(path)  # what the last good sync left for Bank A's checking account
    con.execute(ctdb.UPSERT_BALANCE_LIMIT, ("2026-01-02", "plaid", "tokA-chk", "Checking (3333)", "Bank A", "850.00",
                                            None, "USD", None, None))
    con.commit(); con.close()
    return cfg, path


def test_failed_item_keeps_its_last_good_balance_date(tmp_path, monkeypatch):
    """Plaid still serves cached balances for an item whose login broke. They must not be stored under today's
    date (the next item's commit used to save them), or the stale balance reads as fresh everywhere."""
    cfg, path = _two_items(monkeypatch, tmp_path)
    with pytest.raises(plaid_sync.PlaidError, match="1 item"):
        plaid_sync.sync(cfg, path)
    con = sqlite3.connect(path)
    rows = con.execute("SELECT account_id, snapshot_date, balance FROM balances ORDER BY account_id").fetchall()
    assert [r for r in rows if r[0].startswith("tokA")] == [("tokA-chk", "2026-01-02", "850.00")]
    assert {r[0] for r in rows if r[0].startswith("tokB")} == {"tokB-card1", "tokB-card2", "tokB-chk"}
    assert all(r[1] > "2026-01-02" for r in rows if r[0].startswith("tokB"))
    assert cfg["items"][0].get("cursor") is None and cfg["items"][1]["cursor"] == "c1"


def test_failed_item_is_named_on_an_error_line_for_the_status_file(tmp_path, monkeypatch, capsys):
    """wealth-ledger-sync puts the first `error` line of a failed Plaid sync in the status file."""
    _, path = _two_items(monkeypatch, tmp_path)
    assert plaid_sync.main(["sync", "--db", str(path)]) == 1
    first = [l for l in capsys.readouterr().err.splitlines() if l.startswith("error")][0]
    assert first.startswith("error: Bank A: /transactions/sync: ITEM_LOGIN_REQUIRED")


def test_healthy_items_all_store_todays_balances(tmp_path, monkeypatch):
    cfg, path = _two_items(monkeypatch, tmp_path, fail=())
    plaid_sync.sync(cfg, path)
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(DISTINCT account_id) FROM balances WHERE snapshot_date > '2026-01-02'").fetchone()[0] == 6


def test_cached_balance_is_dated_by_plaids_last_bank_update_and_a_stale_one_warns(tmp_path, monkeypatch, capsys):
    """/accounts/get answers from Plaid's cache with no date; /item/get says when Plaid last reached the bank."""
    def call(cfg, path, body):
        if path == "/item/get":
            return {"status": {"transactions": {"last_successful_update": "2026-01-03T08:00:00Z",
                                                "last_failed_update": "2026-02-01T08:00:00Z"},
                               "investments": {"last_successful_update": "2026-01-05T09:30:00.123Z"}}}
        return {"accounts": ACCOUNTS}

    monkeypatch.setattr(plaid_sync, "call", call)
    con = ctdb.connect(tmp_path / "t.db")
    plaid_sync.account_names({}, {"access_token": "x", "institution": "Bank"}, con)
    assert {r[0] for r in con.execute("SELECT as_of FROM balances")} == {"2026-01-05T09:30:00+00:00"}
    (warn,) = [l for l in capsys.readouterr().err.splitlines() if l.startswith("WARNING")]
    assert warn.startswith("WARNING: Bank: Plaid last got data from the bank on 2026-01-05 (")


def test_fresh_item_does_not_warn_and_no_update_time_leaves_as_of_empty(db, capsys):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    assert plaid_sync.stale_warning("Bank", (now - timedelta(days=3)).isoformat(), now) is None
    assert "5 days ago" in plaid_sync.stale_warning("Bank", (now - timedelta(days=5, hours=1)).isoformat(), now)
    plaid_sync.account_names({}, {"access_token": "x", "institution": "Bank"}, db)  # stub has no "status"
    assert {r[0] for r in db.execute("SELECT as_of FROM balances")} == {None}
    assert "WARNING" not in capsys.readouterr().err


def test_pending_charge_gets_a_date(db):
    t = {"transaction_id": "p1", "account_id": "card1", "amount": 12.5, "pending": True, "date": "2026-11-17",
         "authorized_date": "2026-11-16", "name": "Auth : Toll"}
    row = plaid_sync.to_row(t, {}, "Bank", "2026-11-17T14:00:00+00:00")
    assert row[4] is None and row[5] == "2026-11-16"
    db.execute(ctdb.upsert_sql("txns"), row)
    # a row stored before this fix has neither date: the day the sync first saw it stands in
    old = list(row); old[0], old[5] = "p0", None
    db.execute(ctdb.upsert_sql("txns"), old)
    assert dict(db.execute("SELECT txn_id, date FROM v_ledger")) == {"p1": "2026-11-16", "p0": "2026-11-17"}
