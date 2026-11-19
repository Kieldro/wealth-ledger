"""SimpleFIN response handling (synthetic responses, no network)."""
from datetime import datetime, timezone

import simplefin_sync as sf

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
DAY = 86400


def resp(*accts):
    return {"connections": [{"conn_id": "c1", "name": "Bank A"}, {"conn_id": "c2", "name": "Exchange B"}],
            "accounts": [{"id": f"a{i}", "conn_id": c, "balance-date": int(NOW.timestamp()) - age * DAY}
                         for i, (c, age) in enumerate(accts)]}


def test_connection_stuck_for_days_is_flagged_once():
    (w,) = sf.stale_warnings(resp(("c1", 0), ("c2", 16), ("c2", 20)), NOW)
    assert w.startswith("stale: Connection to Exchange B has not updated since 2026-10-16 (16 days)")
    assert "conn=c2" in w


def test_one_fresh_account_keeps_a_connection_fresh_and_weekends_are_ok():
    assert sf.stale_warnings(resp(("c1", 3), ("c2", 30), ("c2", 1)), NOW) == []


def test_account_left_behind_in_a_fresh_connection_is_named_when_it_holds_money():
    data = resp(("c1", 0), ("c1", 30), ("c1", 30), ("c1", 30))
    data["accounts"][1]["balance"] = "0.00"   # closed: not worth a line
    data["accounts"][2].update(balance="1500.25", name="Old Savings")
    data["accounts"][3]["balance"] = "-20"
    old, owed = sf.stale_warnings(data, NOW)
    assert old.startswith("stale: Account Old Savings at Bank A has not updated since 2026-10-02 (30 days)")
    assert "acct=a2)" in old and "acct=a3)" in owed


def test_backfill_reaches_back_to_where_a_returning_account_stopped():
    from datetime import date
    start, end = date(2026, 10, 18), date(2026, 11, 2)
    known = {"a0": "2026-10-31T10:00:00+00:00", "a1": "2026-09-20T10:00:00+00:00", "a2": "2026-09-01T10:00:00+00:00"}
    fresh = resp(("c1", 0), ("c2", 0), ("c2", 0))
    assert sf.backfill_from(fresh, {"a0": known["a0"]}, {"a1", "a2"}, start, end) is None   # nothing came back
    assert sf.backfill_from(fresh, known, {"a2"}, start, end) == date(2026, 9, 13)   # a1's gap, less 7 days
    assert sf.backfill_from(fresh, known, set(), start, end) == date(2026, 8, 25)    # a2's
    still_stuck = resp(("c1", 0), ("c2", 42))
    assert sf.backfill_from(still_stuck, known, set(), start, end) is None           # wait until it moves
    assert sf.backfill_from(fresh, {"a0": known["a0"], "a1": known["a0"]}, set(), start, end) == date(2026, 8, 4)  # a2 new: 90 days
    long_gap = dict(known, a1="2026-01-01T00:00:00+00:00")
    assert sf.backfill_from(fresh, long_gap, {"a2"}, start, end) == date(2026, 8, 4)  # capped at 90 days


def test_missing_balance_date_is_not_stale():
    data = resp(("c1", 0))
    data["accounts"].append({"id": "x", "conn_id": "c2"})
    assert sf.stale_warnings(data, NOW) == []


def test_account_types_are_guessed_from_names_and_never_overwritten(tmp_path):
    import ctdb
    db = ctdb.connect(str(tmp_path / "t.db"))
    names = ["Everyday Checking", "High Yield Savings", "Travel Rewards Visa", "Roth IRA", "Health Savings Account",
             "Auto Loan", "Mystery Account"]
    for i, n in enumerate(names):
        db.execute(ctdb.UPSERT_BALANCE, ("2026-11-01", "simplefin", f"a{i}", n, "Example Bank", "1.00", None, "USD", None))
    db.execute("INSERT INTO account_kind (account_id, kind) VALUES ('a0', 'savings')")  # the owner's own choice stays
    db.commit()
    guessed, unknown = ctdb.guess_kinds(db)
    assert dict(guessed) == {"High Yield Savings": "savings", "Travel Rewards Visa": "credit", "Roth IRA": "retirement",
                             "Health Savings Account": "hsa", "Auto Loan": "loan"}
    assert unknown == ["Mystery Account"]
    assert db.execute("SELECT kind FROM account_kind WHERE account_id='a0'").fetchone()[0] == "savings"
    assert ctdb.guess_kinds(db) == ([], ["Mystery Account"])


def _accounts(tmp_path, *names):
    import ctdb
    db = ctdb.connect(str(tmp_path / "t.db"))
    for i, n in enumerate(names):
        db.execute(ctdb.UPSERT_BALANCE, ("2026-11-01", "simplefin", f"a{i}", n, "Example Bank", "1.00", None, "USD", None))
    db.commit()
    return ctdb, db


def _kinds(db):
    return dict(db.execute("SELECT account_name, kind FROM v_accounts"))


def test_account_kinds_in_personal_json_name_a_type_and_beat_a_guess(tmp_path, monkeypatch):
    import json
    cfg = tmp_path / "personal.json"
    cfg.write_text(json.dumps({"account_kinds": {"_help": "a comment", "360 Performance%": "savings",
                                                 "Rewards%": "checking", "Rewards Visa%": "credit"}}))
    monkeypatch.setenv("WEALTH_LEDGER_PERSONAL", str(cfg))
    ctdb, db = _accounts(tmp_path, "360 Performance (1234)", "Rewards Checking", "Rewards Visa", "By Hand Savings")
    db.execute("INSERT INTO account_kind (account_id, kind) VALUES ('a3', 'custodial')")  # the owner's own choice
    assert ctdb.guess_kinds(db)[1] == ["360 Performance (1234)"]
    assert _kinds(db)["Rewards Visa"] == "credit"  # guessed
    changed = dict(ctdb.apply_account_kinds(db))
    assert changed["360 Performance (1234)"] == "savings"
    assert _kinds(db) == {"360 Performance (1234)": "savings", "Rewards Checking": "checking",
                          "Rewards Visa": "credit",          # the longest matching pattern wins
                          "By Hand Savings": "custodial"}    # never overwritten
    assert ctdb.apply_account_kinds(db) == [] and ctdb.guess_kinds(db) == ([], [])


def test_account_kinds_rejects_an_unknown_type(tmp_path, monkeypatch):
    import json
    import pytest
    cfg = tmp_path / "personal.json"
    cfg.write_text(json.dumps({"account_kinds": {"Anything%": "piggybank"}}))
    monkeypatch.setenv("WEALTH_LEDGER_PERSONAL", str(cfg))
    ctdb, db = _accounts(tmp_path, "Anything")
    with pytest.raises(ctdb.personal.PersonalConfigError, match="must be one of"):
        ctdb.apply_account_kinds(db)


def test_account_holding_positions_is_an_investment_when_the_name_says_nothing(tmp_path):
    ctdb, db = _accounts(tmp_path, "Account 7", "Account 8")
    db.execute("INSERT INTO holdings (snapshot_date, source, account_id, holding_key, symbol, shares, market_value) "
               "VALUES ('2026-11-01', 'simplefin', 'a0', 'h1', 'VTI', '10', '2500.00')")
    db.commit()
    assert ctdb.guess_kinds(db) == ([("Account 7", "investment")], ["Account 8"])


def test_checking_side_card_payments_and_own_transfers_are_not_spending(tmp_path):
    ctdb, db = _accounts(tmp_path, "Everyday Checking", "Rewards Visa")
    ctdb.guess_kinds(db)
    rows = [("a0", "-300.00", "CARD PAYMENT THANK YOU"), ("a0", "-120.00", "EXAMPLE BANK CREDIT CRD EPAYMENT"),
            ("a0", "-500.00", "ONLINE TRANSFER TO SAV ...1234"), ("a0", "-54.20", "GROCERY MART #123"),
            ("a0", "-25.00", "GIFT CARD STORE"), ("a0", "-40.00", "DEBIT CARD PAYMENT TO EXAMPLE GYM")]
    for i, (aid, amt, desc) in enumerate(rows):
        db.execute("INSERT INTO txns (txn_id, account_id, posted, amount, description, pending, first_seen, last_seen) "
                   "VALUES (?,?,'2026-11-01',?,?,0,'2026-11-01','2026-11-01')", (f"t{i}", aid, amt, desc))
    db.commit()
    got = dict(db.execute("SELECT description, class FROM v_ledger"))
    assert got == {"CARD PAYMENT THANK YOU": "card_payment", "EXAMPLE BANK CREDIT CRD EPAYMENT": "card_payment",
                   "ONLINE TRANSFER TO SAV ...1234": "transfer", "GROCERY MART #123": "spend",
                   "GIFT CARD STORE": "spend", "DEBIT CARD PAYMENT TO EXAMPLE GYM": "spend"}


def test_holdings_view_keeps_an_account_the_feed_skipped_and_drops_an_emptied_one(tmp_path):
    import ctdb
    db = ctdb.connect(str(tmp_path / "t.db"))

    def hold(day, acct, value):
        db.execute("INSERT INTO holdings (snapshot_date, source, account_id, account_name, holding_key, symbol, "
                   "market_value) VALUES (?, 'simplefin', ?, ?, ?, 'VTI', ?)", (day, acct, acct.upper(), acct + "-h", value))

    def bal(day, acct, value):
        db.execute(ctdb.UPSERT_BALANCE, (day, "simplefin", acct, acct.upper(), "Broker", value, None, "USD", None))

    for acct in ("live", "skipped", "emptied"):
        hold("2026-11-01", acct, "100.00")
        bal("2026-11-01", acct, "100.00")
    hold("2026-11-10", "live", "110.00")          # only this account is in the newest snapshot
    bal("2026-11-10", "live", "110.00")
    bal("2026-11-10", "emptied", "0.00")          # rolled over: balance still reported, at zero
    db.commit()
    got = {r[0]: r[1:] for r in db.execute("SELECT account_id, snapshot_date, value, lag_days FROM v_holdings_latest")}
    assert got == {"live": ("2026-11-10", 110.0, 0), "skipped": ("2026-11-01", 100.0, 9)}
    assert ctdb.lagging_holdings(db, "simplefin") == [("SKIPPED", "2026-11-01", 9)]
    assert ctdb.lagging_holdings(db, "plaid") == []


def test_sync_fetches_the_gap_once_when_an_account_comes_back(tmp_path, monkeypatch, capsys):
    import types
    from datetime import timedelta

    import ctdb
    now = datetime.now(timezone.utc)
    path = tmp_path / "t.db"
    db = ctdb.connect(path)
    db.execute(ctdb.UPSERT_BALANCE, ((now - timedelta(days=40)).date().isoformat(), "simplefin", "a0", "Checking",
                                     "Bank A", "5.00", None, "USD", (now - timedelta(days=40)).isoformat()))
    db.commit()
    db.close()
    asked = []

    def fetch(url, ws, we):
        asked.append((now.date() - ws.date()).days)
        gap = (now - timedelta(days=30)).timestamp()
        txns = [{"id": "t-gap", "posted": int(gap), "amount": "-9.00", "description": "Missed"}] if ws.timestamp() < gap else []
        return {"connections": [{"conn_id": "c1", "name": "Bank A"}],
                "accounts": [{"id": "a0", "conn_id": "c1", "name": "Checking", "balance": "7.00", "currency": "USD",
                              "balance-date": int(now.timestamp()), "transactions": txns}]}

    monkeypatch.setattr(sf, "load_access_url", lambda f: "https://u:p@example.test/simplefin")
    monkeypatch.setattr(sf, "fetch_window", fetch)
    args = types.SimpleNamespace(access_file=None, db=path, days=14, start=None, end=None)
    sf.sync(args)
    usual, older = asked  # the usual window, then from 7 days before the old balance-date up to it
    assert usual in (13, 14) and older in (46, 47, 48)  # a day either way: UTC against the local date
    assert "backfill:" in capsys.readouterr().err
    con = ctdb.connect(path)
    assert [r[0] for r in con.execute("SELECT description FROM txns")] == ["Missed"]
    con.close()
    asked.clear()
    sf.sync(args)
    assert asked == [usual]  # the new balance-date is stored: no second backfill
