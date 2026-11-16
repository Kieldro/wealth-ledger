"""Fidelity history import (made-up export files and ledger, no network)."""
import json
from datetime import datetime

import ctdb
import history_import as hi

SWEEP_IN = "PURCHASE INTO CORE ACCOUNT EXAMPLE MONEY MARKET (XMMXX) (Cash)"
SWEEP_OUT = "REDEMPTION FROM CORE ACCOUNT EXAMPLE MONEY MARKET (XMMXX) (Cash)"
CHK, IRA, PLAN, NEW = "ZZZ001111", "ZZZ002222", "PLAN-3333", "ZZZ004444"
ACCOUNTS = [{"acctNum": CHK, "name": "everyday cash", "type": "Brokerage"},
            {"acctNum": IRA, "name": "ROTH IRA", "type": "Brokerage"},
            {"acctNum": PLAN, "name": "EXAMPLE CO", "type": "WPS"},
            {"acctNum": NEW, "name": "Side Account", "type": "Brokerage"}]


def ts(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=hi.ET).timestamp())


def txn(acct, day, net, desc, qty="0", **cat):
    return {"acctNum": acct, "date": ts(day), "dateDetail": {"postedDate": ts(day), "tradedDate": ts(day)},
            "description": desc, "amtDetail": {"net": net}, "quantity": qty,
            "catDetail": cat or {"txnTypeDesc": "Cash Transaction", "txnCatDesc": "Other"}}


def export(tmp_path, *windows, accounts=ACCOUNTS):
    d = tmp_path / "export"
    d.mkdir(exist_ok=True)
    (d / "accounts.json").write_text(json.dumps(accounts))
    for i, txns in enumerate(windows):
        (d / f"w{i}.json").write_text(json.dumps({"data": {"transactions": txns, "cryptoTOAs": []}, "errors": []}))
    return d


def ledger(tmp_path, live=()):
    """A ledger whose live feed knows three of the accounts; `live` = (account_id, date, amount, description)."""
    path = tmp_path / "t.db"
    db = ctdb.connect(str(path))
    for aid, name, kind in (("A-chk", "everyday cash (1111)", "checking"), ("A-ira", "ROTH IRA (2222)", "retirement"),
                            ("A-plan", "EXAMPLE CO 401(K) PLAN (9999)", "retirement")):
        db.execute(ctdb.UPSERT_BALANCE, ("2026-11-01", "simplefin", aid, name, hi.INSTITUTION, "1.00", None, "USD", None))
        db.execute("INSERT INTO account_kind (account_id, kind) VALUES (?,?)", (aid, kind))
    names = dict(db.execute("SELECT account_id, account_name FROM balances"))
    for i, (aid, day, amount, desc) in enumerate(live):
        db.execute(ctdb.upsert_sql("txns"), (f"{aid}:{i}", aid, names[aid], hi.INSTITUTION, day, None, amount, desc,
                                             desc, "", 0, "USD", "t", "t", "simplefin"))
    db.commit()
    db.close()
    return path


def rows(path, where="1"):
    db = ctdb.connect(str(path))
    try:
        return db.execute(f"SELECT account_id, date, amount, description, class, memo FROM v_ledger "
                          f"WHERE source = 'fidelity_web' AND {where} ORDER BY date, description, amount").fetchall()
    finally:
        db.close()


HISTORY = [txn(CHK, "2026-06-30", -42.5, "DEBIT CARD PURCHASE EXAMPLE STORE (Cash)"),
           txn(CHK, "2026-07-01", 1000, "DIRECT DEPOSIT EXAMPLE PAYROLL (Cash)"),
           txn(CHK, "2026-07-01", -1000, SWEEP_IN, "1000"),
           txn(CHK, "2026-07-09", 42.5, SWEEP_OUT, "-42.5"),
           txn(CHK, "2026-07-10", -500, "TRANSFERRED TO VS Z12-345678-1 (Cash)"),
           txn(CHK, "2026-08-02", -20, "DIRECT DEBIT EXAMPLE GYM (Cash)"),       # the live feed has it already
           txn(IRA, "2026-07-10", 500, "TRANSFERRED FROM VS Z98-765432-1 (Cash)"),
           txn(PLAN, "2026-07-15", -0.15, "ADVISOR FEE", txnCatDesc="fees"),
           txn(NEW, "2026-10-01", -75, "YOU BOUGHT EXAMPLE FUND (XFUND) (Cash)", "3")]
LIVE = [("A-chk", "2026-08-01", "-9.99", "DEBIT CARD PURCHASE EXAMPLE CAFE (Cash)"),
        ("A-chk", "2026-08-02", "-20.00", "DIRECT DEBIT EXAMPLE GYM (Cash)")]


def test_accounts_map_by_last4_then_unambiguous_name_else_a_hashed_id():
    led = {"A-chk": "everyday cash (1111)", "A-ira": "ROTH IRA (2222)", "A-plan": "EXAMPLE CO 401(K) PLAN (9999)"}
    m = hi.map_accounts([CHK, IRA, PLAN, NEW], ACCOUNTS, led)
    assert [(m[n].account_id, m[n].matched) for n in (CHK, IRA, PLAN)] == [
        ("A-chk", "last 4"), ("A-ira", "last 4"), ("A-plan", "name")]
    new = m[NEW]
    assert new.matched == "new id" and new.account_id.startswith("fidelity_web:") and new.label == "Side Account (4444)"
    assert NEW not in new.account_id and "4444" not in new.account_id          # no account number in the id
    assert hi.map_accounts([NEW], ACCOUNTS, led)[NEW].account_id == new.account_id  # stable


def test_two_accounts_with_one_name_are_told_apart_by_number_and_never_by_name():
    twins = [{"acctNum": "ZZZ005555", "name": "Custodial (UTMA)"}, {"acctNum": "ZZZ006666", "name": "Custodial (UTMA)"}]
    led = {"A-5": "Custodial (UTMA) (5555)", "A-6": "Custodial (UTMA) (6666)"}
    m = hi.map_accounts(["ZZZ005555", "ZZZ006666"], twins, led)
    assert (m["ZZZ005555"].account_id, m["ZZZ006666"].account_id) == ("A-5", "A-6")
    # same name, digits the ledger doesn't show: a name match would be a guess, so each gets its own id
    m = hi.map_accounts(["ZZZ005555", "ZZZ006666"], twins, {"A-x": "Custodial (UTMA) (0000)"})
    assert {a.matched for a in m.values()} == {"new id"}
    assert m["ZZZ005555"].account_id != m["ZZZ006666"].account_id


def test_kind_of_a_new_account():
    assert hi.guess_kind("Digital Assets", "Crypto") == "crypto"
    assert hi.guess_kind("Individual - Youth Account", "Brokerage") == "custodial"
    assert hi.guess_kind("Individual - TOD", "Brokerage") == "investment"
    assert hi.guess_kind("Owner Only Business", "Brokerage") == "investment"
    assert hi.guess_kind("EXAMPLE CO", "WPS") == "retirement"


def test_only_rows_before_the_first_live_transaction_are_imported(tmp_path, capsys):
    db = ledger(tmp_path, LIVE)
    assert hi.main([str(export(tmp_path, HISTORY)), "--db", str(db)]) == 0
    got = rows(db)
    assert len(got) == 8 and all(r[1] < "2026-08-01" for r in got if r[0] == "A-chk")
    assert not [r for r in got if "GYM" in r[3]]
    assert {r[0] for r in got} == {"A-chk", "A-ira", "A-plan", hi.new_account_id(NEW)}
    out = capsys.readouterr().out
    assert "everyday cash (1111): import 5 rows 2026-06-30..2026-07-10; cutoff 2026-08-01; held back 1" in out
    assert "cutoff none (no live transactions)" in out and "inserted=8" in out
    assert CHK not in out and NEW not in out  # account numbers are never printed


def test_sign_descriptions_and_classes_match_the_live_feed(tmp_path):
    db = ledger(tmp_path, LIVE)
    hi.main([str(export(tmp_path, HISTORY)), "--db", str(db)])
    by_desc = {r[3]: r for r in rows(db)}
    assert by_desc["DEBIT CARD PURCHASE EXAMPLE STORE (Cash)"][2:5] == ("-42.50", "DEBIT CARD PURCHASE EXAMPLE STORE (Cash)", "spend")
    assert by_desc["DIRECT DEPOSIT EXAMPLE PAYROLL (Cash)"][2::2] == ("1000.00", "income")
    assert by_desc[SWEEP_IN][2::2] == ("-1000.00", "transfer")      # a cash sweep is never spend
    assert by_desc[SWEEP_OUT][2::2] == ("42.50", "transfer")        # ... nor income
    assert by_desc["TRANSFERRED TO VS XXX-XXX678-1 (Cash)"][4] == "transfer"   # account number masked like the feed
    assert by_desc["TRANSFERRED FROM VS XXX-XXX432-1 (Cash)"][4] == "transfer"
    assert by_desc["fees"][2:] == ("-0.15", "fees", "investment", "ADVISOR FEE")  # workplace plan: category word
    assert by_desc["YOU BOUGHT EXAMPLE FUND (XFUND) (Cash)"][4] == "investment"
    con = ctdb.connect(str(db))
    assert con.execute("SELECT kind, note FROM account_kind WHERE account_id = ?", (hi.new_account_id(NEW),)).fetchone() \
        == ("investment", "fidelity_web history import")
    assert con.execute("SELECT kind FROM account_kind WHERE account_id = 'A-chk'").fetchone() == ("checking",)
    assert con.execute("SELECT DISTINCT institution, pending, currency FROM txns WHERE source='fidelity_web'").fetchall() \
        == [(hi.INSTITUTION, 0, "USD")]
    con.close()


def test_rerun_is_idempotent_and_overlapping_windows_count_once(tmp_path, capsys):
    db = ledger(tmp_path, LIVE)
    coffee = txn(CHK, "2026-07-03", -4, "DEBIT CARD PURCHASE EXAMPLE CAFE (Cash)")
    # the same day is in two windows; the two coffees bought that day are two transactions, not four
    d = export(tmp_path, HISTORY + [coffee, coffee], [coffee, dict(coffee), HISTORY[0]])
    hi.main([str(d), "--db", str(db)])
    first = rows(db)
    assert len(first) == 10 and len([r for r in first if "CAFE" in r[3]]) == 2
    capsys.readouterr()
    hi.main([str(d), "--db", str(db)])
    assert "inserted=0 already_there=10" in capsys.readouterr().out
    assert rows(db) == first


def test_dry_run_writes_nothing(tmp_path, capsys):
    db = ledger(tmp_path, LIVE)
    before = db.read_bytes()
    assert hi.main([str(export(tmp_path, HISTORY)), "--db", str(db), "--dry-run"]) == 0
    assert db.read_bytes() == before
    out = capsys.readouterr().out
    assert "dry run: would import 8 of 9 rows, nothing written" in out and "cutoff 2026-08-01" in out


def test_overlap_mismatch_is_reported_and_not_imported(tmp_path, capsys):
    db = ledger(tmp_path, LIVE)   # the live feed has a cafe charge the export lacks
    hi.main([str(export(tmp_path, HISTORY)), "--db", str(db)])
    out = capsys.readouterr().out
    assert "fidelity 1 rows sum -20.00 | ledger 2 rows sum -29.99 | MISMATCH (1 rows)" in out
    assert "ledger only:   2026-08-01        -9.99 DEBIT CARD PURCHASE EXAMPLE CAFE (Cash)" in out
    assert "1 one-sided overlap rows match as is, 0 only with the sign flipped" in out
    (tmp_path / "ok").mkdir()
    hi.main([str(export(tmp_path, HISTORY)), "--db", str(ledger(tmp_path / "ok", LIVE[1:]))])
    assert "fidelity 1 rows sum -20.00 | ledger 1 rows sum -20.00 | ok" in capsys.readouterr().out


def test_flipped_sign_is_refused(tmp_path, capsys):
    db = ledger(tmp_path, LIVE)
    flipped = [dict(t, amtDetail={"net": -t["amtDetail"]["net"]}) for t in HISTORY]
    assert hi.main([str(export(tmp_path, flipped)), "--db", str(db)]) == 1
    assert "sign flipped; nothing imported" in capsys.readouterr().err
    assert rows(db) == []


def test_rows_a_live_feed_covers_later_are_removed_on_rerun(tmp_path, capsys):
    db = ledger(tmp_path, LIVE)
    d = export(tmp_path, HISTORY)
    hi.main([str(d), "--db", str(db)])
    new_id = hi.new_account_id(NEW)
    assert [r[1] for r in rows(db, f"account_id = '{new_id}'")] == ["2026-10-01"]
    con = ctdb.connect(str(db))   # the feed starts covering the IRA earlier, and picks up the new account
    con.execute(ctdb.UPSERT_BALANCE, ("2026-11-02", "simplefin", "A-new", "Side Account (4444)", hi.INSTITUTION,
                                      "1.00", None, "USD", None))
    for tid, aid, name, day in (("n1", "A-new", "Side Account (4444)", "2026-09-15"), ("n2", "A-ira", "ROTH IRA (2222)", "2026-07-05")):
        con.execute(ctdb.upsert_sql("txns"), (tid, aid, name, hi.INSTITUTION, day, None, "1.00", "x", "x", "", 0,
                                              "USD", "t", "t", "simplefin"))
    con.commit()
    con.close()
    capsys.readouterr()
    hi.main([str(d), "--db", str(db)])
    assert "removed_past_cutoff=2" in capsys.readouterr().out
    assert rows(db, f"account_id IN ('{new_id}', 'A-new', 'A-ira')") == []
    con = ctdb.connect(str(db))
    assert con.execute("SELECT COUNT(*) FROM account_kind WHERE account_id = ?", (new_id,)).fetchone() == (0,)
    con.close()


def test_bad_folder_is_an_error_not_a_traceback(tmp_path, capsys):
    assert hi.main([str(tmp_path), "--db", str(tmp_path / "t.db"), "--dry-run"]) == 1
    assert "no .json files" in capsys.readouterr().err
