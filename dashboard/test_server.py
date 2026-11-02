"""Dashboard API helpers against a small synthetic database (no real data)."""
import json
from datetime import date

import pytest

import ctdb
from insights import prices
from dashboard import server

TODAY = date.today().isoformat()


@pytest.fixture
def dbfile(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    con = ctdb.connect(path)
    rows = [  # account_id, kind, source, balance, available, as_of
        ("c1", "credit", "plaid", "-250.00", "750.00", None),          # limit 1000, 25% used
        ("c2", "credit", "simplefin", "-100.00", "0", f"{TODAY}T08:00:00"),  # SimpleFIN 0 = unknown
        ("c3", "credit", "plaid", "40.00", "500.00", None),            # credit balance
        ("x1", "crypto", "simplefin", "10.00", None, "2026-01-02T18:10:35"),
    ]
    for aid, kind, src, bal, avail, as_of in rows:
        con.execute("INSERT INTO account_kind VALUES (?,?,NULL)", (aid, kind))
        con.execute(ctdb.UPSERT_BALANCE, (TODAY, src, aid, f"Test {aid} (1234)", "Bank", bal, avail, "USD", as_of))
    con.commit()
    con.close()
    monkeypatch.setattr(server, "DB", str(path))
    return path


def test_freshness_uses_bank_date_then_snapshot():
    r = {"as_of": "2026-10-16T18:10:35", "snapshot_date": "2026-10-27"}
    server.freshness(r, today=date(2026, 10, 27))
    assert (r["as_of"], r["age_days"], r["stale"]) == ("2026-10-16 18:10", 11, True)
    r = {"as_of": None, "snapshot_date": "2026-10-25"}
    server.freshness(r, today=date(2026, 10, 27))
    assert (r["as_of"], r["age_days"], r["stale"]) == ("2026-10-25", 2, False)
    r = {"as_of": "2026-10-24T00:00:00", "snapshot_date": "2026-10-27"}
    server.freshness(r, today=date(2026, 10, 27))
    assert r["stale"] is False  # exactly STALE_DAYS old is still fresh


def test_cards_owed_available_and_utilization(dbfile):
    out = server.api_cards({})
    cards = {c["account_name"][:7]: c for c in out["cards"]}
    c1, c2, c3 = cards["Test c1"], cards["Test c2"], cards["Test c3"]
    assert (c1["owed"], c1["available"], c1["limit"], c1["util_pct"]) == (250.0, 750.0, 1000.0, 25.0)
    assert c2["owed"] == 100.0 and c2["available"] is None and c2["util_pct"] is None
    assert c3["owed"] == -40.0 and c3["util_pct"] == 0.0
    assert out["total"] == 310.0
    # totals only over cards that report available credit: owed 250 of limit 1000 + 500
    assert c1["limit_basis"] == "estimate" and c2["limit_basis"] is None  # estimate = owed + available
    assert out["available"] == 1250.0 and out["util_pct"] == pytest.approx(16.7)


def test_cards_use_feed_and_manual_credit_limits(dbfile):
    con = ctdb.connect(dbfile)
    # c4: the feed reports a limit (no available); c5: no feed figure, limit from personal.example.json credit_limits
    con.execute("INSERT INTO account_kind VALUES ('c4','credit',NULL), ('c5','credit',NULL)")
    con.execute(ctdb.UPSERT_BALANCE_LIMIT, (TODAY, "plaid", "c4", "Test c4 (1234)", "Bank", "-500.00", None, "USD",
                                            None, "2000.00"))
    con.execute(ctdb.UPSERT_BALANCE, (TODAY, "simplefin", "c5", "Example Rewards Card (0000)", "Bank", "-100.00",
                                      "0", "USD", None))
    con.commit(); con.close()
    out = server.api_cards({})
    cards = {c["account_name"][:7]: c for c in out["cards"]}
    c4, c5 = cards["Test c4"], cards["Example"]
    assert (c4["limit"], c4["available"], c4["util_pct"], c4["limit_basis"]) == (2000.0, 1500.0, 25.0, "plaid")
    assert (c5["limit"], c5["available"], c5["util_pct"], c5["limit_basis"]) == (5000.0, 4900.0, 2.0, "manual")
    assert out["total"] == 910.0
    # overall utilization over cards with a known limit: owed 250+0+500+100 of 1000+500+2000+5000
    assert (out["limit"], out["limit_cards"], out["available"], out["util_pct"]) == (8500.0, 4, 7650.0, 10.0)


def test_accounts_flag_stale_feeds(dbfile):
    accts = {a["account_name"][:7]: a for a in server.api_accounts({})["accounts"]}
    assert accts["Test x1"]["stale"] is True
    assert accts["Test c2"]["stale"] is False and accts["Test c2"]["age_days"] == 0


def test_period_changes_mtd_ytd():
    s = [{"date": "2025-12-30", "synced": 100.0}, {"date": "2026-01-15", "synced": 150.0},
         {"date": "2026-02-27", "synced": 170.0}, {"date": "2026-03-02", "synced": 200.0}]
    ch = server.period_changes(s)
    assert ch["mtd"] == {"delta": 30.0, "vs": "2026-02-27"}  # last snapshot on/before Feb 28
    assert ch["ytd"] == {"delta": 100.0, "vs": "2025-12-30"}
    assert ch["1"]["delta"] is None and ch["30"]["delta"] is None


def test_period_changes_short_history():
    ch = server.period_changes([{"date": "2026-10-16", "synced": 1.0}, {"date": "2026-10-27", "synced": 3.0}])
    assert ch["mtd"]["delta"] is None and ch["ytd"]["delta"] is None
    assert ch["mtd"]["note"] == "history starts 2026-10-16"
    assert server.period_changes([]) == {}


@pytest.fixture
def auth(tmp_path, monkeypatch):
    f = tmp_path / "auth.json"
    f.write_text(json.dumps({"username": "u", "salt": server._b64e(b"s" * 16), "hash": server._b64e(b"h" * 32),
                             "n": 2, "r": 1, "p": 1, "session_secret": "ab" * 32}))
    f.chmod(0o600)
    monkeypatch.setattr(server, "AUTH_FILE", str(f))
    monkeypatch.setattr(server, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    return server.Auth()


def test_session_renews_after_a_day_and_keeps_working(auth, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(server.time, "time", lambda: now)
    cookie = auth.new_session()
    assert auth.renew(cookie) is None  # fresh: nothing to do
    now += server.SESSION_TTL - 3600  # an hour before it would expire
    fresh = auth.renew(cookie)
    assert fresh and fresh != cookie and auth.check(fresh)
    now += 7200  # original expiry has passed; the renewed cookie is still good
    assert auth.check(cookie) is None and auth.check(fresh)


def test_revoked_session_is_not_renewed(auth, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(server.time, "time", lambda: now)
    cookie = auth.new_session()
    auth.revoke([cookie])
    now += 2 * server.RENEW_AFTER
    assert auth.renew(cookie) is None


def test_account_txns_lists_only_that_account(dbfile):
    con = ctdb.connect(dbfile)
    for i, (aid, d, amt, desc, pending) in enumerate([("c1", "2026-09-01", "-20.00", "COFFEE #12", 0),
                                                      ("c1", "2026-09-03", "100.00", "PAYMENT THANK YOU", 0),
                                                      ("c1", "2026-09-05", "-5.00", "SNACK", 1),
                                                      ("c2", "2026-09-02", "-999.00", "OTHER CARD", 0)]):
        con.execute(ctdb.upsert_sql("txns"), (f"t{i}", aid, aid, "Bank", d, None, amt, desc, None, None, pending,
                                              "USD", "x", "x", "test"))
    con.commit()
    con.close()
    r = server.api_account_txns({"id": ["c1"]})
    assert [t["date"] for t in r["txns"]] == ["2026-09-05", "2026-09-03", "2026-09-01"]
    assert r["account"]["account_id"] == "c1" and r["first"] == "2026-09-01" and not r["truncated"]
    assert r["money_in"] == 100 and r["money_out"] == -20  # pending excluded from totals
    assert server.api_account_txns({"id": ["nope"]})["error"] == "unknown account"
    assert all("account_id" in a for a in server.api_accounts({})["accounts"])


def test_trend_sheet_points_before_first_daily(dbfile, monkeypatch):
    line = {"line": "Home", "used": 1000.0, "type": "asset", "from": "sheet"}
    monkeypatch.setattr(server, "networth", lambda refresh=False: {"data": {"lines": [line]}, "error": None})
    con = ctdb.connect(dbfile)
    con.executemany("INSERT INTO networth_history VALUES (?,?,?,?,?,?)", [
        ("2025-07-01", 2000.456, 2100.0, 99.544, "Statements 2025, column D", "2026-10-01T00:00:00+00:00"),
        ("2024-01-01", 1500.0, None, None, "Statements 2024, column G", "2026-10-01T00:00:00+00:00"),
        (TODAY, 9.0, None, None, "Statements 2026, column B", "2026-10-01T00:00:00+00:00"),  # not before daily
        ("2023-01-01", None, None, None, "Statements 2023, column F", "2026-10-01T00:00:00+00:00"),
    ])
    con.commit()
    con.close()
    out = server.api_trend({})
    assert {"series", "sheet_only_total", "note", "first_daily", "sheet_points"} <= out.keys()
    assert out["series"] == [{"date": TODAY, "synced": -310.0, "total": 690.0}]
    assert out["first_daily"] == TODAY
    assert out["sheet_points"] == [
        {"date": "2024-01-01", "net_worth": 1500.0, "source": "Statements 2024, column G"},
        {"date": "2025-07-01", "net_worth": 2000.46, "source": "Statements 2025, column D"}]


def test_trend_without_history_table_or_snapshots(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    sqlite3.connect(path).executescript(
        "CREATE TABLE balances (snapshot_date, source, account_id, account_name, institution, balance, available,"
        " currency, as_of); CREATE TABLE account_kind (account_id, kind, note); CREATE TABLE superseded (account_id);")
    monkeypatch.setattr(server, "DB", str(path))
    monkeypatch.setattr(server, "networth", lambda refresh=False: {"data": None, "error": "x"})
    out = server.api_trend({})
    assert out["series"] == [] and out["first_daily"] is None and out["sheet_points"] == []


def test_trend_prices_cover_every_date_and_spot(dbfile, monkeypatch):
    monkeypatch.setattr(server, "networth", lambda refresh=False: {"data": None, "error": "x"})
    con = ctdb.connect(dbfile)
    con.executemany("INSERT INTO networth_history (date, net_worth, source) VALUES (?,?,?)", [
        ("2020-04-19", 1.0, "s"),   # before any price: left out
        ("2020-04-25", 2.0, "s"),   # a Saturday: Friday's gold close
    ])
    con.executemany("INSERT INTO prices VALUES (?,?,?,?)", [
        ("2020-04-20", "BTC", 6800.0, "coinbase"), ("2020-04-24", "BTC", 7500.0, "coinbase"),
        ("2020-04-25", "BTC", 7550.0, "coinbase"), ("2020-04-24", "XAU", 1720.0, "yahoo GC=F"),
        ("2026-10-30", "XAU", 3900.0, "yahoo GC=F"), (TODAY, "BTC", 80000.0, "coinbase")])
    con.execute("INSERT INTO valuations VALUES (?,?,?,?,?,?)", (
        TODAY, "Total metals value", 1.0, "spot: swissquote",
        json.dumps({"metals": {"gold": {"oz": 1, "spot": 3950.5, "source": "swissquote"}}}), TODAY))
    con.execute("INSERT INTO valuations VALUES (?,?,?,?,?,?)", (
        TODAY, "Total cryptocurrency (USD)", 1.0, "x",
        json.dumps({"prices": {"BTC": {"usd": 79000.0, "source": "coingecko"}}}), TODAY))
    con.commit()
    con.close()
    out = server.api_trend({})
    assert [p["date"] for p in out["sheet_points"]] == ["2020-04-19", "2020-04-25"]
    assert out["prices"]["BTC"] == {"2020-04-25": 7550.0, TODAY: 80000.0}
    assert out["prices"]["XAU"] == {"2020-04-25": 1720.0, TODAY: 3900.0}
    # BTC has today's row; today's gold only exists as the metals provider's live spot
    assert out["spot"]["BTC"] == {"usd": 80000.0, "date": TODAY, "source": "coinbase"}
    assert out["spot"]["XAU"] == {"usd": 3950.5, "date": TODAY, "source": "valuations (swissquote)"}
    json.dumps(out)


def test_trend_prices_without_prices_table(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    sqlite3.connect(path).executescript(
        "CREATE TABLE balances (snapshot_date, source, account_id, account_name, institution, balance, available,"
        " currency, as_of); CREATE TABLE account_kind (account_id, kind, note); CREATE TABLE superseded (account_id);")
    monkeypatch.setattr(server, "DB", str(path))
    monkeypatch.setattr(server, "networth", lambda refresh=False: {"data": None, "error": "x"})
    out = server.api_trend({})
    assert out["prices"] == {sym: {} for sym in prices.SYMBOLS} and out["spot"] == {sym: None for sym in prices.SYMBOLS}


def test_trend_prices_for_units_without_a_live_spot(dbfile, monkeypatch):
    """Rial: stored as USD per rial with enough digits to survive; silver falls back to the metals provider's spot;
    copper has no live spot, so its latest close is used even when it is days old."""
    monkeypatch.setattr(server, "networth", lambda refresh=False: {"data": None, "error": "x"})
    con = ctdb.connect(dbfile)
    con.executemany("INSERT INTO prices VALUES (?,?,?,?)", [
        ("2026-10-28", "IRR", 5.3750e-07, "yahoo IRR=X"), ("2026-10-28", "HG", 6.5, "yahoo HG=F"),
        ("2026-10-28", "XAG", 48.0, "yahoo SI=F")])
    con.execute("INSERT INTO valuations VALUES (?,?,?,?,?,?)", (
        TODAY, "Total metals value", 1.0, "spot: swissquote",
        json.dumps({"metals": {"silver": {"oz": 1, "spot": 49.12, "source": "swissquote"}}}), TODAY))
    con.commit()
    con.close()
    out = server.api_trend({})
    assert out["spot"]["IRR"]["usd"] == 5.375e-07 and out["spot"]["HG"] == {"usd": 6.5, "date": "2026-10-28",
                                                                             "source": "yahoo HG=F"}
    assert out["spot"]["XAG"] == {"usd": 49.12, "date": TODAY, "source": "valuations (swissquote)"}


def test_bench_dates_weekly_then_daily_plus_every_trend_date():
    g = server.bench_dates(["2024-01-03", "2024-01-05", "2026-10-30"], "2026-11-02")
    assert g[:3] == ["2024-01-03", "2024-01-05", "2024-01-10"]  # weekly steps keep the odd trend date too
    daily_from = date(2026, 11, 2).toordinal() - server.BENCH_DAILY_DAYS
    recent = [d for d in g if date.fromisoformat(d).toordinal() >= daily_from + 7]
    assert len(recent) == len(set(recent)) == server.BENCH_DAILY_DAYS - 6 and g[-1] == "2026-11-02"
    assert server.bench_dates([], "2026-11-02") == []


def test_trend_benchmarks_are_overlays_not_units(dbfile, monkeypatch):
    """S&P 500 / Nasdaq ride in "benchmarks" only (the page's unit picker reads "prices" and "spot"), aligned with
    benchmarks.dates; units with a price history are there too so an overlay can be priced in the shown unit."""
    monkeypatch.setattr(server, "networth", lambda refresh=False: {"data": None, "error": "x"})
    monkeypatch.setattr(server, "BENCH_DAILY_DAYS", 3)
    con = ctdb.connect(dbfile)
    con.execute("INSERT INTO networth_history (date, net_worth, source) VALUES ('2020-04-24', 2.0, 's')")
    con.executemany("INSERT INTO prices VALUES (?,?,?,?)", [
        ("2020-04-24", "SPX", 2836.7, "yahoo ^GSPC"), ("2020-04-24", "XAU", 1720.0, "yahoo GC=F"),
        ("2020-04-30", "SPX", 2912.4, "yahoo ^GSPC"), ("2020-04-24", "KWD", 3.2, "yahoo KWD=X")])
    con.commit()
    con.close()
    out = server.api_trend({})
    b = out["benchmarks"]
    assert b["dates"][:2] == ["2020-04-24", "2020-05-01"] and b["dates"][-1] == TODAY
    assert set(b["prices"]) == set(prices.SYMBOLS + prices.BENCHMARKS) - prices.LATEST_ONLY
    assert all(len(v) == len(b["dates"]) for v in b["prices"].values())
    assert b["prices"]["SPX"][:2] == [2836.7, 2912.4] and b["prices"]["XAU"][1] == 1720.0
    assert b["prices"]["IXIC"] == [None] * len(b["dates"])
    assert "SPX" not in out["prices"] and "SPX" not in out["spot"] and "IXIC" not in out["spot"]
    json.dumps(out)
