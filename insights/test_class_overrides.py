"""txn_class_overrides on a synthetic card feed (no real data): a hand-set class beats txn_rules and the fallback,
and a 'disputed' charge counts as no spending."""
from datetime import date

import ctdb
from insights import overdraft


def add(db, tid, posted, amount, desc, account="card"):
    db.execute(ctdb.upsert_sql("txns"), (tid, account, "Card", "Bank", posted, None, f"{amount:.2f}", desc, None, None,
                                         0, "USD", "2026-05-01T00:00:00", "2026-05-01T00:00:00", "plaid"))


def classes(db):
    return dict(db.execute("SELECT txn_id, class FROM v_ledger"))


def setup(tmp_path):
    db = ctdb.connect(tmp_path / "t.db")
    db.execute("INSERT INTO account_kind VALUES ('card','credit',NULL)")
    add(db, "pay", "2026-05-02", 300.00, "PAYMENT THANK YOU")    # a txn_rules match: card_payment
    add(db, "buy", "2026-05-03", -40.00, "GROCER")                # fallback: spend
    add(db, "ret", "2026-05-04", 15.00, "GROCER RETURN")          # fallback on a card: refund
    add(db, "f1", "2026-05-05", -250.00, "ONLINE STORE")          # fraud, reversed by the issuer
    add(db, "f2", "2026-05-06", -125.00, "ONLINE STORE")
    db.commit()
    return db


def test_override_beats_rule_and_fallback(tmp_path):
    db = setup(tmp_path)
    assert classes(db) == {"pay": "card_payment", "buy": "spend", "ret": "refund", "f1": "spend", "f2": "spend"}
    db.executemany("INSERT INTO txn_class_overrides (txn_id, class, note) VALUES (?,?,?)",
                   [("pay", "transfer", "beats the rule"), ("ret", "income", "beats the fallback"),
                    ("f1", "disputed", "fraud, dispute won"), ("f2", "disputed", "fraud, dispute won")])
    db.commit()
    assert classes(db) == {"pay": "transfer", "buy": "spend", "ret": "income", "f1": "disputed", "f2": "disputed"}
    assert db.execute("SELECT created FROM txn_class_overrides WHERE txn_id='f1'").fetchone()[0]  # defaulted
    db.close()
    db = ctdb.connect(tmp_path / "t.db")  # the next sync's connect() keeps them and rebuilds the view
    assert classes(db)["f1"] == "disputed"


def test_disputed_counts_as_no_spending(tmp_path):
    db = setup(tmp_path)
    db.execute("INSERT INTO txn_class_overrides (txn_id, class) VALUES ('f1','disputed'), ('f2','disputed')")
    db.commit()
    spend = db.execute("SELECT -SUM(amt) FROM v_ledger WHERE class='spend'").fetchone()[0]
    assert spend == 40.00
    assert db.execute("SELECT COUNT(*) FROM v_ledger WHERE class IN ('spend','income','refund')").fetchone()[0] == 2
    # the card's recent charge rate leaves them out too (net of the refund, over SPEND_DAYS)
    card = {"account_id": "card", "balance": -25.00}
    assert overdraft._daily_charges(db, card, date(2026, 5, 31)) == (40.00 - 15.00) / overdraft.SPEND_DAYS
    # and so does walking the balance back: their reversal never reached the feed, so they never moved it
    # owed 25 now = owed before + 40 charged - 15 refunded - 300 paid, so 300 before (675 if f1/f2 had counted)
    assert overdraft._owed_at(db, card, date(2026, 5, 1)) == 300.00
    assert overdraft._owed_at(db, card, date(2026, 5, 1), overdraft._posted(db, card)) == 300.00
    rows = overdraft._posted(db, card)
    assert {c for _, _, c in rows} == {"card_payment", "spend", "refund"}
