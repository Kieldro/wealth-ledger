"""insights/holding_prices.py on synthetic holdings and closes (no network)."""
from datetime import date

import pytest

import ctdb
from insights import holding_prices as hp

# Fri 2026-11-06, then the weekend, then Mon 2026-11-09
CLOSES = {"AAA": [("2026-11-05", 10.0), ("2026-11-06", 11.0), ("2026-11-09", 12.0)],
          "BBB": [("2026-11-06", 50.0)]}


def test_weekend_carries_the_friday_close():
    pos = [("AAA", 100.0, 10.0, 1000.0)]
    for day, close in (("2026-11-06", 11.0), ("2026-11-07", 11.0), ("2026-11-08", 11.0), ("2026-11-09", 12.0)):
        value, used, fallback = hp.revalue(1000.0, pos, CLOSES, day)
        assert value == pytest.approx(100 * close) and not fallback
    assert hp.revalue(1000.0, pos, CLOSES, "2026-11-08")[1] == "2026-11-06"  # Sunday's value is as of Friday's close
    assert hp.close_on(CLOSES["AAA"], "2026-11-04") is None  # before the first close


def test_money_market_is_a_dollar_a_share_and_never_fetched():
    assert hp.is_cash("SPAXX") and hp.is_cash("FDRXX") and hp.is_cash("VMFXX") and not hp.is_cash("AAA")
    value, used, fallback = hp.revalue(1500.0, [("SPAXX", 500.0, 1.0, 500.0), ("AAA", 100.0, 10.0, 1000.0)],
                                       CLOSES, "2026-11-09")
    assert value == pytest.approx(500 + 1200) and used == "2026-11-09" and not fallback


def test_remainder_of_the_balance_is_kept_as_cash():
    # balance 1300 = 1000 of AAA (at the feed's price) + 300 not in any position (pending cash, an unlisted holding)
    value, _, _ = hp.revalue(1300.0, [("AAA", 100.0, 10.0, 1000.0)], CLOSES, "2026-11-09")
    assert value == pytest.approx(300 + 100 * 12.0)
    # a blank-symbol sweep keeps its market value and isn't reported as a fallback
    value, _, fallback = hp.revalue(1400.0, [("AAA", 100.0, 10.0, 1000.0), ("", None, None, 100.0)], CLOSES,
                                    "2026-11-09")
    assert value == pytest.approx(300 + 100 + 1200) and not fallback


def test_no_close_keeps_the_feed_price_and_says_so():
    pos = [("AAA", 100.0, 10.0, 1000.0), ("ZZZ", 10.0, 7.0, 70.0)]
    value, used, fallback = hp.revalue(1070.0, pos, CLOSES, "2026-11-09")
    assert value == pytest.approx(1200 + 70) and fallback == {"ZZZ"} and used == "2026-11-09"
    # a close older than MAX_CLOSE_AGE days (delisted, halted) is not used either
    assert hp.revalue(500.0, [("BBB", 10.0, 50.0, 500.0)], CLOSES, "2026-11-20") == (500.0, None, {"BBB"})
    # nor one far off the feed's own price: another security under that ticker, or a split since
    assert hp.revalue(1000.0, [("AAA", 100.0, 3.0, 300.0)], CLOSES, "2026-11-09")[2] == {"AAA"}


@pytest.fixture
def con(tmp_path):
    db = ctdb.connect(tmp_path / "h.db")
    for aid, kind in (("ira", "retirement"), ("cust", "custodial"), ("cx", "crypto")):
        db.execute("INSERT INTO account_kind VALUES (?,?,NULL)", (aid, kind))
    for aid, sym, d in (("ira", "AAA", "2026-11-02"), ("ira", "AAA", "2026-11-09"), ("ira", "BF.B", "2026-11-04"),
                        ("ira", "SPAXX", "2026-11-02"), ("ira", "", "2026-11-02"), ("cust", "CCC", "2026-11-02"),
                        ("cx", "DDD", "2026-11-02")):
        db.execute(ctdb.UPSERT_HOLDING, (d, "simplefin", aid, aid, "Broker", sym or "sweep", sym, sym, "1", "1", "1",
                                         "0", "USD"))
    db.commit()
    yield db
    db.close()


def test_update_backfills_once_then_only_the_recent_days(con):
    asked = []

    def fetch(sym, s, e):
        asked.append((sym, s.isoformat(), e.isoformat()))
        return {"2026-10-30": 10.0, "2026-11-06": 11.0, "2026-11-09": 12.0}
    rep = hp.update(con, today=date(2026, 11, 10), fetch=fetch, sleep=lambda s: None)
    # only counted accounts, no money market or blank symbol; each from a week before its first snapshot
    assert asked == [("AAA", "2026-10-26", "2026-11-10"), ("BF.B", "2026-10-28", "2026-11-10")]
    assert rep == {"symbols": 2, "rows": 6, "failed": []}
    assert con.execute("SELECT source FROM security_prices WHERE symbol = 'BF.B' LIMIT 1").fetchone() == ("yahoo BF-B",)
    asked.clear()
    hp.update(con, today=date(2026, 11, 13), fetch=fetch, sleep=lambda s: None)
    # AAA is still held: its last few days again; BF.B left after 11-04 and its closes reach past that: nothing
    assert asked == [("AAA", "2026-11-06", "2026-11-13")]
    asked.clear()  # a week on with no new snapshot holding them: both have left every account, nothing to fetch
    assert hp.update(con, today=date(2026, 11, 20), fetch=fetch, sleep=lambda s: None)["symbols"] == 0 and not asked
    assert hp.load(con, ["AAA", "ZZZ"]) == {"AAA": [("2026-10-30", 10.0), ("2026-11-06", 11.0), ("2026-11-09", 12.0)]}


def test_update_reports_a_symbol_without_closes_and_fails_only_when_none_worked(con):
    def fetch(sym, s, e):
        if sym == "BF.B":
            raise RuntimeError("yahoo BF-B: no result")
        return {"2026-11-09": 12.0}
    assert hp.update(con, today=date(2026, 11, 10), fetch=fetch, sleep=lambda s: None)["failed"] == ["BF.B"]
    with pytest.raises(RuntimeError, match="no close fetched"):
        hp.update(con, today=date(2026, 11, 10), fetch=lambda *a: 1 / 0, sleep=lambda s: None)
    assert hp.update(ctdb.connect(":memory:"), fetch=lambda *a: 1 / 0) == {"symbols": 0, "rows": 0, "failed": []}


def test_yahoo_spelling():
    assert hp.yahoo_symbol("BF.B") == "BF-B" and hp.yahoo_symbol("AAA") == "AAA"
