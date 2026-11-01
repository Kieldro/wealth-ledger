"""ctdb.flag_late_duplicates on a synthetic card feed (no real data)."""
import ctdb


def add(db, tid, posted, amount, desc, first_seen):
    db.execute(ctdb.upsert_sql("txns"), (tid, "card", "Card", "Bank", posted, None, f"{amount:.2f}", desc, None, None,
                                         0, "USD", first_seen, first_seen, "plaid"))


def test_late_copy_hidden_but_first_import_repeats_kept(tmp_path):
    db = ctdb.connect(tmp_path / "t.db")
    # first import: two identical coffees on consecutive days are real
    add(db, "c1", "2026-01-05", -3.56, "Coffee", "2026-02-01T00:00:00")
    add(db, "c2", "2026-01-06", -3.56, "Coffee", "2026-02-01T00:00:00")
    # later: a charge, then the same charge re-delivered two weeks after its date
    add(db, "h1", "2026-03-16", -1256, "Insurer", "2026-03-18T00:00:00")
    add(db, "h2", "2026-03-18", -1256, "Insurer", "2026-04-01T00:00:00")
    # a real repeat that arrives promptly is kept
    add(db, "s1", "2026-03-20", -22.49, "Credits", "2026-03-21T00:00:00")
    add(db, "s2", "2026-03-22", -22.49, "Credits", "2026-03-23T00:00:00")
    db.commit()
    assert ctdb.flag_late_duplicates(db) == [("h2", "h1")]
    assert ctdb.flag_late_duplicates(db) == []  # idempotent
    kept = {r[0] for r in db.execute("SELECT txn_id FROM v_ledger")}
    assert kept == {"c1", "c2", "h1", "s1", "s2"}
