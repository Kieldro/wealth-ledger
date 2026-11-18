"""Dashboard API helpers against a small synthetic database (no real data)."""
import json
from datetime import date, timedelta

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


def test_period_changes_leave_out_accounts_linked_during_the_period():
    """A brokerage account linked on 3/1 with $50,000 already in it is not a $50,000 gain for the month."""
    s = [{"date": "2026-02-27", "synced": 1000.0, "accounts": {"chk": 1000.0}},
         {"date": "2026-03-01", "synced": 51100.0, "accounts": {"chk": 1100.0, "brk": 50000.0, "empty": 0.0}},
         {"date": "2026-03-02", "synced": 51800.0, "accounts": {"chk": 1300.0, "brk": 50500.0, "empty": 0.0}}]
    ch = server.period_changes(s)
    assert ch["mtd"] == {"delta": 300.0, "vs": "2026-02-27", "excluded": 1, "excluded_total": 50500.0}
    assert ch["1"] == {"delta": 700.0, "vs": "2026-03-01"}  # both accounts existed yesterday: nothing left out


def test_synced_series_carries_per_account_balances_and_the_trend_does_not_send_them(dbfile, monkeypatch):
    con = ctdb.connect(dbfile)
    con.execute("INSERT INTO account_kind VALUES ('s1','savings',NULL)")
    con.execute(ctdb.UPSERT_BALANCE, ("2026-01-05", "test", "c1", "Test c1 (1234)", "Bank", "-200.00", None, "USD", None))
    con.execute(ctdb.UPSERT_BALANCE, ("2026-01-06", "test", "s1", "Savings (9)", "Bank", "900.00", None, "USD", None))
    con.commit()
    series = server.synced_series()
    assert series[0] == {"date": "2026-01-05", "synced": -200.0, "accounts": {"c1": -200.0}}
    assert series[1] == {"date": "2026-01-06", "synced": 700.0, "accounts": {"c1": -200.0, "s1": 900.0}}
    monkeypatch.setattr(server, "networth", lambda refresh=False: {"data": None, "error": "x"})
    assert all(set(p) <= {"date", "synced", "total"} for p in server.api_trend({})["series"])


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


def test_running_balance_skips_sweeps_and_accounts_it_cannot_follow(dbfile):
    con = ctdb.connect(dbfile)
    con.execute("INSERT INTO account_kind VALUES ('k1','checking',NULL)")
    con.execute(ctdb.UPSERT_BALANCE, (TODAY, "simplefin", "k1", "Test k1 (1234)", "Bank", "1000.00", None, "USD", None))
    later = (date.fromisoformat(TODAY) + timedelta(days=1)).isoformat()
    for i, (aid, d, amt, desc) in enumerate([("k1", "2026-09-01", "500.00", "DIRECT DEPOSIT ACME"),
                                             ("k1", "2026-09-02", "-500.00", "PURCHASE INTO CORE ACCOUNT FDIC"),
                                             ("k1", "2026-09-03", "-200.00", "DIRECT DEBIT RENT"),
                                             ("k1", later, "-50.00", "NOT IN THE BALANCE YET"),
                                             ("x1", "2026-09-03", "-5.00", "YOU BOUGHT BTC")]):
        con.execute(ctdb.upsert_sql("txns"), (f"r{i}", aid, aid, "Bank", d, None, amt, desc, None, None, 0,
                                              "USD", "x", "x", "test"))
    con.commit()
    con.close()
    got = {t["description"]: t["balance_after"] for t in server.api_account_txns({"id": ["k1"]})["txns"]}
    assert got == {"NOT IN THE BALANCE YET": None, "DIRECT DEBIT RENT": 1000, "PURCHASE INTO CORE ACCOUNT FDIC": 1200,
                   "DIRECT DEPOSIT ACME": 1200}  # the sweep moved nothing
    assert server.api_account_txns({"id": ["x1"]})["txns"][0]["balance_after"] is None  # market-valued account
    recent = {t["description"]: t["balance_after"] for t in server.api_spending({})["recent"]}
    assert recent["DIRECT DEBIT RENT"] == 1000 and recent["YOU BOUGHT BTC"] is None


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
    assert [t["balance_after"] for t in r["txns"]] == [None, -250, -350]  # walked back from -250; pending has none
    assert server.api_account_txns({"id": ["nope"]})["error"] == "unknown account"
    assert all("account_id" in a for a in server.api_accounts({})["accounts"])
    sparks = [a["spark"] for a in server.api_accounts({})["accounts"]]
    assert all(len(p) == 2 and isinstance(p[0], str) for s in sparks for p in s)
    assert all(s == sorted(s, key=lambda p: p[0]) for s in sparks)
    assert {c["account_id"] for c in server.api_cards({})["cards"]} == {"c1", "c2", "c3"}  # card rows open the same view


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


def test_line_source_live_estimate_sheet():
    assert server.line_source({"from": "live (2 accounts)"}) == "live"
    assert server.line_source({"from": "live reading (exchange-api USD balance)"}) == "live"
    assert server.line_source({"from": "estimate (spot)"}) == "est"
    assert server.line_source({"from": "estimate (exchange-api USD balance, 2026-01-01)"}) == "est"
    assert server.line_source({"from": "sheet"}) == "sheet"


def test_live_exchange_reading_stays_in_the_trend_constant(dbfile, monkeypatch):
    """A valuation read live from an exchange gets the live badge, but it isn't a synced account: the trend's
    daily series doesn't contain it, so it must stay in the constant added on top."""
    lines = [{"line": "Checking", "type": "asset", "used": 1000.0, "from": "live", "synced": True},
             {"line": "Exchange", "type": "asset", "used": 50.0, "from": "live reading (exchange-api USD balance)",
              "synced": False},
             {"line": "Home", "type": "asset", "used": 300.0, "from": "estimate (avm, 2026-01-01)", "synced": False},
             {"line": "Loan", "type": "liability", "used": 20.0, "from": "sheet", "synced": False},
             {"line": "Old cache", "type": "asset", "used": 7.0, "from": "live (no sheet row)"}]  # no key: by prefix
    data = {"lines": lines, "assets": 1357.0, "liabilities": 20.0, "net_worth": 1337.0}
    monkeypatch.setattr(server, "networth", lambda refresh=False: {"data": data, "error": None, "fetched": 0.0})
    monkeypatch.setattr(server, "synced_series", lambda: [])
    monkeypatch.setattr(server, "STATUS", "/nonexistent")
    assert server.api_summary({})["sheet_only_total"] == 330.0
    assert server.api_trend({})["sheet_only_total"] == 330.0


def test_crypto_groups_by_wallet_and_coin(dbfile):
    detail = {"holdings": [
        {"wallet": "W1", "coin": "BTC", "qty": 0.5, "price": 80000.0, "value": 40000.0, "qty_source": "onchain", "counted": True},
        {"wallet": "W1", "coin": "DUST", "qty": 1e-9, "price": 1.0, "value": 0.0, "qty_source": "onchain", "counted": True},
        {"wallet": "W2", "coin": "BTC", "qty": 0.25, "price": 80000.0, "value": 20000.0, "qty_source": "exchange-api", "counted": True},
        {"wallet": "W2", "coin": "USD", "qty": 50.0, "price": 1.0, "value": 50.0, "qty_source": "exchange-api",
         "counted": False, "why_not_counted": "own sheet line"},
        {"wallet": "W2", "coin": "NEW", "qty": 3.0, "price": None, "value": 0.0, "qty_source": "exchange-api", "counted": False}],
        "by_coin": {"BTC": {"qty": 0.75, "price": 80000.0, "value": 60000.0}}, "sheet_total": 59000.0, "warnings": ["no price for W2 NEW"]}
    con = ctdb.connect(dbfile)
    con.execute("INSERT INTO valuations VALUES (?,?,?,?,?,?)",
                (TODAY, "Total cryptocurrency (USD)", 60000.0, "estimate", json.dumps(detail), TODAY))
    con.commit()
    con.close()
    c = server.api_crypto({})
    assert c["total"] == 60000.0 and c["sheet_total"] == 59000.0 and c["warnings"] == ["no price for W2 NEW"]
    assert [(w["wallet"], w["value"]) for w in c["wallets"]] == [("W1", 40000.0), ("W2", 20000.0)]
    assert [h["coin"] for h in c["wallets"][1]["holdings"]] == ["BTC", "USD", "NEW"]  # uncounted last, dust dropped
    assert c["coins"] == [{"coin": "BTC", "qty": 0.75, "price": 80000.0, "value": 60000.0, "pct": 100.0, "where": [
        {"wallet": "W1", "qty": 0.5, "value": 40000.0, "qty_source": "onchain"},
        {"wallet": "W2", "qty": 0.25, "value": 20000.0, "qty_source": "exchange-api"}]}]
    assert [(x["where"], x["type"], x["btc"]) for x in c["btc"]["locations"]] == [
        ("W1", "self-custody", 0.5), ("W2", "self-custody", 0.25)]


def test_btc_locations_add_bitcoin_funds(dbfile):
    con = ctdb.connect(dbfile)
    for aid, kind in (("ira", "retirement"), ("kid", "custodial"), ("cb", "crypto")):
        con.execute("INSERT INTO account_kind VALUES (?,?,NULL)", (aid, kind))
    con.executemany("INSERT INTO holdings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (TODAY, "simplefin", "ira", "IRA (1234)", "Fid", "k1", "FBTC", "Fidelity Bitcoin", "100", "80", "8000", None, "USD"),
        (TODAY, "simplefin", "ira", "IRA (1234)", "Fid", "k2", "VTI", "Total market", "10", "300", "3000", None, "USD"),
        (TODAY, "simplefin", "kid", "UTMA (1)", "Fid", "k3", "FBTC", "Fidelity Bitcoin", "1", "80", "80", None, "USD"),
        (TODAY, "simplefin", "cb", "Wallet (ab-12)", "CB", "k4", "BTC", "Bitcoin", "0.1", "0", "0", None, "USD")])
    con.commit()
    con.close()
    wallets = [{"wallet": "Coinbase", "holdings": [
        {"coin": "BTC", "qty": 0.05, "value": 4000.0, "qty_source": "exchange-api", "counted": True}]}]
    b = server.btc_locations(wallets, 80000.0)
    assert [(x["where"], x["type"], x["btc"], round(x["pct"])) for x in b["locations"]] == [
        ("IRA (1234)", "fund", 0.1, 67), ("Coinbase", "exchange", 0.05, 33)]  # custodial + crypto-kind rows skipped
    assert b["btc"] == pytest.approx(0.15) and b["value"] == 12000.0
    assert [t["type"] for t in b["by_type"]] == ["fund", "exchange"]


def test_crypto_without_a_valuation(dbfile):
    assert server.api_crypto({}) == {"total": None, "wallets": [], "coins": [], "transfers": []}


def test_crypto_lists_transfers_newest_first_with_usd(dbfile):
    con = ctdb.connect(dbfile)
    con.execute("INSERT INTO prices VALUES ('2026-03-01','BTC',80000,'x')")
    con.executemany("INSERT INTO crypto_transfers VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        ("W", "BTC", "t1", "in", "2026-03-01T10:00:00-06:00", 0.5, None, "a", None, "", "x"),
        ("W", "BTC", "t2", "out", "2026-03-02T10:00:00-06:00", 0.1, 0.0001, "b", "Other", "gift", "x")])
    con.commit()
    con.close()
    t = server.api_crypto({})["transfers"]
    assert [(x["txid"], x["usd_value"], x["memo"]) for x in t] == [("t2", 8000.0, "gift"), ("t1", 40000.0, "")]


def test_status_flags_a_sync_that_stopped_finishing(tmp_path, monkeypatch):
    """The status file is rewritten only when a sync finishes, so an old "rc=0" must not read as healthy."""
    from datetime import datetime, timezone
    f = tmp_path / "status"
    monkeypatch.setattr(server, "STATUS", str(f))
    now = datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc)

    f.write_text("last run 2026-11-11T08:15:02-06:00 rc=0\nOverdraft risk: Checking has $5\n")
    assert server.read_status(now) == {"last_run": "2026-11-11T08:15:02-06:00", "rc": 0,
                                       "problems": ["Overdraft risk: Checking has $5"]}
    f.write_text("last run 2026-11-10T08:15:02-06:00 rc=0\n")       # yesterday morning, 30 hours ago: still fine
    assert server.read_status(now)["problems"] == []
    f.write_text("last run 2026-11-06T08:15:02-06:00 rc=0\nOverdraft risk: Checking has $5\n")
    st = server.read_status(now)
    assert st["rc"] == 0 and len(st["problems"]) == 2
    assert st["problems"][0].startswith("No sync has finished since 2026-11-06 (5 days ago)")
    f.write_text("last run 2026-11-06T08:15:02 rc=0\n")             # no UTC offset: read as local time
    assert server.read_status(now)["problems"][0].startswith("No sync has finished since 2026-11-06")
    f.write_text("")
    assert server.read_status(now) == {"last_run": None, "rc": None,
                                       "problems": ["The status file doesn't say when the sync last finished"]}
    f.write_text("something unexpected\n")
    assert server.read_status(now)["problems"] == ["The status file doesn't say when the sync last finished",
                                                   "something unexpected"]
    f.unlink()
    assert server.read_status(now)["problems"][0].startswith("status file unreadable")


def test_summary_shows_networth_warnings_with_the_sync_problems(tmp_path, monkeypatch):
    f = tmp_path / "status"
    f.write_text(f"last run {TODAY}T08:15:02+00:00 rc=0\n")
    monkeypatch.setattr(server, "STATUS", str(f))
    data = {"lines": [], "assets": 0, "liabilities": 0, "net_worth": 0, "sheet_net_worth": 0,
            "warnings": ["Net worth: Savings (1111) was last updated 2026-01-01 and is still counted"]}
    monkeypatch.setattr(server, "networth", lambda refresh=False: {"data": data, "error": None, "fetched": 0.0})
    monkeypatch.setattr(server, "synced_series", lambda: [])
    assert server.api_summary({})["status"]["problems"] == data["warnings"]


def test_page_reloads_itself_and_says_when_numbers_are_old():
    """Guard on the page source (behaviour is checked in a browser by hand): an open tab must re-fetch once its
    data is older than the cache lifetime, and must say so when the numbers are over a day old or a reload failed."""
    html = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    for needle in ('document.addEventListener("visibilitychange", reloadIfOld)', "setInterval(reloadIfOld",
                   'addEventListener("pageshow"', "data.summary?.cache_ttl_s", "function agePill(s)",
                   'id="unrefreshed"', 'id="loaderrors"'):
        assert needle in html, needle


def test_page_shows_loading_and_never_leaves_a_blank_after_a_failed_request():
    """Guard on the page source (behaviour is checked in a headless browser): a bar while any request is in flight,
    placeholders where a value has not arrived, a time limit on every request, and a failed or timed-out request
    leaves words and a Retry button rather than an empty cell. Every card belongs to exactly one request."""
    import re
    html = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    for needle in ('<div id="loadbar" role="progressbar" aria-label="Loading"></div>', '$("loadbar").hidden = !inflight;',
                   "const TIMEOUT_MS = 20e3, SLOW_MS = 75e3", "signal: ac.signal", "setTimeout(() => ac.abort(), timeout)",
                   "no answer after ${Math.round(timeout / 1000)} seconds", "can't reach the server",
                   'id="nw"><span class="skel" role="status" aria-label="Loading"></span>',
                   'id="assets"><span class="skel"', 'id="liabs"><span class="skel"',
                   '<span class="pill"><span class="spin" aria-hidden="true"></span>Loading…</span>',
                   "Couldn't load this section.", "Couldn't refresh this section. Showing what loaded earlier.",
                   '<button type="button" data-retry="${k}">Retry</button>', 'e.target.closest("button[data-retry]")',
                   'setLoad(k, had ? "stale" : "error", e.message)', 'id="acctretry"', "body.slowload .loadbox .lbl::after",
                   '.card[data-load="loading"] > .secbody > :not(.loadbox), .card[data-load="error"] > .secbody > :not(.loadbox) { display: none !important; }',
                   '.card[data-load="error"] > .sechd .kf::before { content: "Couldn\'t load";'):
        assert needle in html, needle
    assert '>—</div>' not in html[html.index('id="hero"'):html.index('<div class="grid"')]  # no dash standing in for a number
    still = html[html.rindex("@media (prefers-reduced-motion: reduce)"):html.index("</style>")]
    assert still.count("animation: none") == 3 and "#loadbar::before" in still and ".spin" in still and ".skel" in still
    parts = html[html.index("const PARTS = {"):html.index("const SKEL =")]
    owned = [i for cards in re.findall(r"cards: \[([^\]]*)\]", parts) for i in re.findall(r'"(\w+)"', cards)]
    grid = html[html.index('<div class="grid"'):html.index('id="totop"')]
    assert sorted(owned) == sorted(re.findall(r'<section class="card span-\d+" id="(\w+)"', grid)) and len(owned) == 14
    assert len(re.findall(r'url: "/api/\w+", fn: render\w+', parts)) == 12
    for slow in ("summary", "trend", "budget"):  # these wait on a sheet read, which the server allows 60 s
        assert re.search(rf"\b{slow}: \{{[^}}]*slow: true\}}", parts), slow


def test_page_says_sync_problem_in_words_not_an_exit_code():
    """Guard on the page source: a failed sync reads "Sync problem" with the status file's problem lines in the
    tooltip (and, with none, one line of our own); the bare "(rc=1)" is gone."""
    html = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    assert "(rc=" not in html and "rc=\" +" not in html
    for needle in ('st.rc ? `Sync problem<span class="hide-sm"> · last ran ${esc(fmtShort(st.last_run))}</span>`',
                   '(bad ? "\\n" + sync.map(p => "• " + p).join("\\n") : "")', '<span class="dot ${bad ? "bad" : ""}"></span>',
                   "The last sync ended with an error (exit code ${st.rc}), so some accounts may not have updated.",
                   'if (bad) $("syncpill").onclick'):
        assert needle in html, needle


def test_input_ages_names_the_oldest_live_balance_and_estimate():
    today = date(2026, 10, 27)
    lines = [{"line": "Checking", "used": 1000.0, "from": "live", "as_of": "2026-10-27"},
             {"line": "Savings", "used": 500.0, "from": "live", "as_of": "2026-10-20T08:00:00"},
             {"line": "Card", "used": 50.0, "from": "live (no sheet row)", "as_of": "2026-10-26"},
             {"line": "Closed", "used": 0.0, "from": "live", "as_of": "2026-01-01"},           # nothing counted
             {"line": "Exchange", "used": 9.0, "from": "live reading (exchange-api USD balance)", "as_of": "2026-10-27"},
             {"line": "Home", "used": 300.0, "from": "estimate (avm, 2026-10-08)", "as_of": "2026-10-08"},
             {"line": "Metals", "used": 80.0, "from": "estimate (spot, 2026-10-27)", "as_of": "2026-10-27"},
             {"line": "Cash", "used": 20.0, "from": "sheet", "as_of": None}]
    assert server.input_ages(lines, today) == {
        "live": {"old": 2, "of": 4, "oldest": "2026-10-20", "line": "Savings", "age_days": 7},
        "est": {"old": 1, "of": 2, "oldest": "2026-10-08", "line": "Home", "age_days": 19}}
    assert server.input_ages([l for l in lines if l["as_of"] in ("2026-10-27", None)], today) == {}
    assert server.input_ages([{"line": "Old cache", "used": 5.0, "from": "live"}], today) == {}  # no as_of key


def test_page_sections_fold_have_a_nav_label_and_a_key_figure_slot():
    """Guard on the page source (behaviour is checked in a browser by hand): every section can be jumped to and
    folded, the drafts come first, and the alert and warnings stay one line until opened."""
    import re
    html = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    grid = html[html.index('<div class="grid"'):html.index('id="totop"')]
    heads = re.findall(r'<section class="card span-\d+" id="(\w+)" data-nav="([\w ]+)">', grid)
    assert len(heads) == grid.count("<section") == 14 and heads[0] == ("draftscard", "Drafts")
    assert len({i for i, _ in heads}) == 14
    for needle in ('const SEC_KEY = "wl.sections"', 'fig.id = "kf-" + sec.id', 'id="jump"', 'goSec("draftscard")',
                   "data warning", '<details><summary>Why</summary>', 'id="moredrafts"'):
        assert needle in html, needle
    for sid in ("draftscard", "alloccard", "trendcard", "acctscard", "holdscard", "cardscard", "spendcard", "budget",
                "subscard", "cryptocard", "ira", "rentalcard"):
        assert f'kf("{sid}"' in html, sid


def test_page_cards_reorder_by_drag_or_buttons_and_remember_the_order():
    """Guard on the page source (behaviour is checked in a browser by hand): the order is the viewer's own, it can be
    reset, every card gets labelled move buttons, and the wide-screen layout rules stand aside for a custom order."""
    html = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    for needle in ('const ORDER_KEY = "wl.order"', "localStorage.setItem(ORDER_KEY", "localStorage.removeItem(ORDER_KEY)",
                   'id="reorderbtn"', 'id="reorderbtn2"', 'id="reorderdone"', '>Reset order</button>',
                   'b.setAttribute("aria-label", `Move ${sec.dataset.nav} ${dir}`)', '[[-1, "up", "▲"], [1, "down", "▼"]]',
                   'e.pointerType !== "touch"', 'id="reordermsg" role="status"', "SEC_IDS.includes(id)",
                   ".grid:not(.custom) > :is(#alloccard, #trendcard, #liabcard) { order: -1; }",
                   ".grid.custom > section.card { grid-column: span var(--w, 12); }"):
        assert needle in html, needle
    assert "draggable=" not in html and "dragstart" not in html  # pointer events: HTML5 drag-and-drop fails on touch


def test_page_trend_has_a_one_week_range_before_one_month():
    html = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    rng = html[html.index('id="trendrng"'):html.index('id="trendchg"')]
    assert rng.index('data-r="1W"') < rng.index('data-r="1M"') and '>1W</button>' in rng
    assert 'const TREND_RANGES = ["1W", "1M",' in html  # a remembered choice is only kept when it's in this list
    assert 'if (r === "1W") return new Date(y, m, d - 7, 12).getTime();' in html


def test_page_marks_an_estimated_correlation_so_it_cannot_pass_for_a_measured_one():
    """Guard on the page source: a range the daily snapshots don't cover still gets a figure, but with "≈", an "est"
    tag, its own style, a visible note and a title that says estimate. The first daily date comes from the data."""
    import re
    html = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    for needle in ("function benchCorrEst(t, u, pts, k)", 'el.classList.toggle("est", !!est)', 'est ? "≈" + num : num',
                   'el.insertAdjacentHTML("beforeend", "<small>est</small>")', 'id="trendcorrnote" hidden',
                   "net worth between snapshots is taken as a straight line, so these mostly reflect", ": estimate only, ≈${num}",
                   "this range starts on a straight line${onLine}", "const path = lead ? [lead, ...real] : real",
                   "daily = pts.filter(p => p.kind === \"daily\"), d0 = daily[0]", "c.n < 8 ? \"few\""):
        assert needle in html, needle
    est = re.search(r"\.bench button \.corr\.est \{([^}]*)\}", html).group(1)
    assert "font-style: italic" in est and "opacity" in est and "underline dashed" in est
    js = html[html.index("function benchCorr("):html.index("const pctStr")]
    assert not re.search(r"20\d\d-\d\d-\d\d", js)  # no date written into the rule


def test_allocation_splits_the_crypto_total_by_wallet(dbfile, monkeypatch):
    """The crypto total drills down to one line per wallet; an exchange's own cash row is labelled as cash. When
    the valuation isn't the number in use, the total stays one line. A wallet read in full today is live even when
    another wallet's typed-in amount makes the total an estimate."""
    detail = {"holdings": [
        {"wallet": "W1", "coin": "BTC", "qty": 0.5, "price": 80000.0, "value": 40000.0, "counted": True,
         "qty_source": "sheet"},
        {"wallet": "W2", "coin": "BTC", "qty": 0.25, "price": 80000.0, "value": 20000.0, "counted": True,
         "qty_source": "exchange-api"},
        {"wallet": "W2", "coin": "USD", "qty": 50.0, "price": 1.0, "value": 50.0, "counted": False}]}
    con = ctdb.connect(dbfile)
    con.execute("INSERT INTO valuations VALUES (?,?,?,?,?,?)",
                (TODAY, server.CRYPTO_LINE, 60000.0, "estimate", json.dumps(detail), TODAY))
    con.commit()
    con.close()
    lines = [{"line": server.CRYPTO_LINE, "type": "asset", "used": 60000.0, "from": "estimate (x)", "synced": False},
             {"line": "W2", "type": "asset", "used": 50.0, "from": "live reading (exchange-api USD balance)",
              "synced": False}]
    data = {"lines": lines, "assets": 60050.0, "liabilities": 0, "net_worth": 60050.0}
    monkeypatch.setattr(server, "networth", lambda refresh=False: {"data": data, "error": None, "fetched": 0.0})
    monkeypatch.setattr(server, "synced_series", lambda: [])
    monkeypatch.setattr(server, "STATUS", "/nonexistent")
    monkeypatch.setattr(server, "CRYPTO_CASH_ROWS", {"W2"})
    monkeypatch.setattr(server, "EXACT", {"W2": "crypto"})

    def crypto():
        c = next(c for c in server.api_summary({})["categories"] if c["key"] == "crypto")
        return c["value"], [(l["line"], l["value"]) for l in c["lines"]]
    assert crypto() == (60050.0, [("W1", 40000.0), ("W2", 20000.0), ("W2 cash", 50.0)])
    c = next(c for c in server.api_summary({})["categories"] if c["key"] == "crypto")
    assert [l["source"] for l in c["lines"]] == ["est", "live", "live"]
    lines[0]["used"] = 59000.0  # fell back to the sheet's figure
    assert crypto() == (59050.0, [(server.CRYPTO_LINE, 59000.0), ("W2 cash", 50.0)])


def test_icon_bytes_serves_only_png_files_from_the_icon_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ICON_DIR", str(tmp_path))
    (tmp_path / "test-bank.png").write_bytes(b"\x89PNG\r\n\x1a\nrest")
    (tmp_path / "not-an-image.png").write_text("<html>")
    assert server.icon_bytes("test-bank").startswith(b"\x89PNG")
    assert server.icon_bytes("not-an-image") is None and server.icon_bytes("missing") is None
    assert server.ICON_PATH.fullmatch("/icons/test-bank.png") and not server.ICON_PATH.fullmatch("/icons/../auth.png")


def _txns(dbfile, rows):
    con = ctdb.connect(dbfile)
    con.execute("INSERT OR IGNORE INTO account_kind VALUES ('k1','checking',NULL)")
    for i, (d, amt, desc, pending) in enumerate(rows):
        con.execute(ctdb.upsert_sql("txns"), (f"s{i}", "k1", "k1", "Bank", d, None, amt, desc, None, None, pending,
                                              "USD", "x", "x", "test"))
    con.commit()
    con.close()


def test_spending_income_net_and_partial_months(dbfile, monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 13)
    monkeypatch.setattr(server, "date", Today)
    monkeypatch.setattr(server, "LEDGER_START", "2026-09-20")
    _txns(dbfile, [("2026-09-10", "-999.00", "BEFORE THE LEDGER STARTS", 0),
                   ("2026-09-25", "-100.00", "GROCER", 0), ("2026-09-30", "2000.00", "PAYROLL ACME", 0),
                   ("2026-10-01", "-1500.00", "DIRECT DEBIT RENT", 0), ("2026-10-15", "3000.00", "PAYROLL ACME", 0),
                   ("2026-10-16", "-500.00", "PURCHASE INTO CORE ACCOUNT FDIC", 0),  # a sweep: transfer, not spend
                   ("2026-11-02", "-40.00", "GROCER", 0), ("2026-11-03", "-7.00", "PENDING COFFEE", 1)])
    sp = server.api_spending({})
    by = {m["month"]: m for m in sp["monthly"]}
    assert list(by) == ["2026-09", "2026-10", "2026-11"]
    assert (by["2026-09"]["spent"], by["2026-09"]["income"], by["2026-09"]["net"], by["2026-09"]["partial"]) == (100, 2000, 1900, True)
    assert (by["2026-10"]["spent"], by["2026-10"]["income"], by["2026-10"]["net"], by["2026-10"]["partial"]) == (1500, 3000, 1500, False)
    assert (by["2026-11"]["spent"], by["2026-11"]["income"], by["2026-11"]["partial"]) == (40, 0, True)  # pending left out
    assert by["2026-10"]["n_income"] == 1 and by["2026-10"]["n"] == 1


def test_spending_daily_pace_this_month_and_last(dbfile, monkeypatch):
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 3)
    monkeypatch.setattr(server, "date", Today)
    monkeypatch.setattr(server, "LEDGER_START", "2026-10-02")
    _txns(dbfile, [("2026-10-01", "-80.00", "BEFORE THE LEDGER STARTS", 0), ("2026-10-02", "-10.00", "A", 0),
                   ("2026-10-02", "-5.00", "B", 0), ("2026-10-31", "-20.00", "C", 0), ("2026-10-31", "900.00", "PAY", 0),
                   ("2026-11-02", "-30.00", "D", 0), ("2026-11-03", "-9.00", "PENDING", 1),
                   ("2026-11-04", "-99.00", "AFTER TODAY", 0)])
    sp = server.api_spending({})
    assert sp["today"] == "2026-11-03"
    last, this = sp["daily"]["2026-10"], sp["daily"]["2026-11"]
    assert len(last) == 31 and last[0] is None and last[1] == 15 and last[30] == 20 and sum(last[1:]) == 35
    assert this == [0, 30, 0]  # through today only; pending not counted


def test_page_has_privacy_mode_pace_and_per_day_hooks():
    """Guard on the page source (behaviour is checked in a headless browser): privacy mode is remembered and masks
    through the formatters and a text pass; spending has the pace chart and savings chips; budget rows show per day."""
    html = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    for needle in ('id="privbtn"', 'const PRIV_KEY = "wl.privacy"', "if (privacy) return masked(u);", "new MutationObserver",
                   'id="pace"', "function renderPace(sp)", 'id="savechips"', "function renderIncome(sp)", "perDay(c.remaining)"):
        assert needle in html, needle


# ---------- connecting SimpleFIN from the page ----------
def _token(url: str) -> str:
    import base64
    return base64.b64encode(url.encode()).decode()


@pytest.fixture
def connect(tmp_path, monkeypatch):
    import threading
    sys_path_mod = __import__("sys")
    sys_path_mod.path.insert(0, str(__import__("pathlib").Path(server.ROOT) / "simplefin"))
    import simplefin_sync
    claimed, started = [], []
    monkeypatch.setattr(server, "WEB_CONNECT", True)
    monkeypatch.setattr(server, "ACCESS_FILE", str(tmp_path / "simplefin_access_url"))
    monkeypatch.setattr(server, "_conn", {"running": False, "error": None})
    monkeypatch.setattr(simplefin_sync, "claim", lambda token, path: (claimed.append(token), path.write_text("x"))[0])
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self))
    return claimed, started


def test_connect_is_off_unless_enabled(connect, monkeypatch):
    monkeypatch.setattr(server, "WEB_CONNECT", False)
    assert server.connect_state() is None
    assert server.start_connect(_token("https://beta-bridge.simplefin.org/simplefin/claim/abc"))[0] == 404
    assert connect == ([], [])


@pytest.mark.parametrize("bad", ["", "not base64!", _token("http://beta-bridge.simplefin.org/claim/abc"),
                                 _token("https://evil.example.com/claim"), _token("https://simplefin.org.evil.example/x")])
def test_connect_refuses_anything_but_a_simplefin_claim_url(connect, bad):
    assert server.start_connect(bad)[0] == 400
    assert connect == ([], [])


def test_connect_claims_once_then_starts_the_first_sync(connect):
    claimed, started = connect
    assert server.connect_state()["needed"]
    tok = _token("https://beta-bridge.simplefin.org/simplefin/claim/abc")
    assert server.start_connect(tok + "\n ") == (200, {"ok": True})
    assert claimed == [tok] and len(started) == 1
    assert server.connect_state() == {"needed": False, "running": True, "error": None}
    assert server.start_connect(tok)[0] == 409  # an existing connection can't be replaced from the page
    assert claimed == [tok]


def test_sections_without_data_are_hidden_for_a_sheetless_ledger(monkeypatch):
    monkeypatch.setattr(server.personal, "has_sheet", lambda: False)
    monkeypatch.setattr(server.personal, "section", lambda name: {})
    assert set(server.hidden_sections()) == {"budget", "cryptocard", "ira", "rentalcard"}
    monkeypatch.setattr(server.personal, "has_sheet", lambda: True)
    monkeypatch.setattr(server.personal, "section", lambda name: {"x": 1})
    assert server.hidden_sections() == []


# ---------- sign-in page ----------
def test_login_page_keeps_its_form_contract_and_its_script_matches_the_csp():
    import base64, hashlib, re
    html = server.LOGIN_HTML
    for needle in ('<form method="post" action="/login"', 'name="username"', 'name="password" type="password"',
                   'autocomplete="username"', 'autocomplete="current-password"', "{{error}}", "{{username}}", "{{markets}}"):
        assert needle in html, needle
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == html.count("<script") == 2  # theme (in <head>) and form behaviour; nothing loaded from elsewhere
    for body in scripts:
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in server.CSP_LOGIN.split("script-src ")[1].split(";")[0]
    assert "default-src 'none'" in server.CSP_LOGIN and "http" not in server.CSP_LOGIN


def test_login_page_shows_the_error_and_keeps_the_username_escaped(monkeypatch):
    monkeypatch.setattr(server, "login_markets", lambda: '<p class="mkt">closes</p>')  # no ledger DB behind this test
    sent = []
    h = server.Handler.__new__(server.Handler)
    h._send = lambda code, body, ctype, headers=(), csp=None, cache=None: sent.append((code, body.decode(), csp))
    h._login_page()
    h._login_page(401, "Wrong <b>username</b>", username='x"><script>alert(1)</script>{{error}}{{markets}}')
    (ok, plain, csp), (code, page, _) = sent
    assert ok == 200 and 'role="alert"' not in plain and 'value=""' in plain and csp == server.CSP_LOGIN
    assert code == 401 and 'role="alert"' in page and "Wrong &lt;b&gt;username&lt;/b&gt;" in page
    assert 'value="x&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;{{error}}{{markets}}"' in page  # typed text is never expanded
    assert page.count("<script") == 2 and "{{username}}" not in page
    assert plain.count('<p class="mkt">closes</p>') == page.count('<p class="mkt">closes</p>') == 1
    assert "Wealth Ledger" in plain and "Built by Keolabs" in plain


def test_theme_default_comes_from_personal_json_and_every_theme_is_on_both_pages(monkeypatch):
    index = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    for page in (index, server.LOGIN_HTML):
        assert page.count('<html lang="en">') == 1 and "prefers-color-scheme" not in page  # dark only
        assert "/^(%s)$/" % "|".join(server.THEMES) in page  # the saved choice is applied before first paint
        for t in server.THEMES[1:]:
            assert f':root[data-theme="{t}"]' in page, t
    for t in server.THEMES:
        assert f'data-t="{t}"' in index, t  # the picker
    monkeypatch.setattr(server.personal, "section", lambda name: {"theme": "rose"} if name == "dashboard" else {})
    assert server.default_theme() == "rose" and '<html lang="en" data-theme="rose">' in server.with_theme(index)
    assert server.index_csp(server.with_theme(index).encode()) == server.index_csp(index.encode())
    for bad in ({}, {"theme": "neon"}, {"theme": ['"><script>']}):
        monkeypatch.setattr(server.personal, "section", lambda name, bad=bad: bad)
        assert server.default_theme() == "graphite" and server.with_theme(index) == index

    def missing(name):
        raise server.personal.PersonalConfigError("no personal.json")
    monkeypatch.setattr(server.personal, "section", missing)
    assert server.with_theme(server.LOGIN_HTML) == server.LOGIN_HTML  # the sign-in page still opens


# ---------- market closes on the sign-in page ----------
@pytest.fixture(autouse=True)
def _fresh_login_markets(monkeypatch):
    monkeypatch.setattr(server, "_login_markets", None)  # the memo must not carry from one test into the next


def _day(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _prices(dbfile, rows):
    con = ctdb.connect(dbfile)
    con.executemany("INSERT INTO prices VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()


def _mkt(html: str) -> str:
    """The market line without its icons, so a test reads as the text and change classes a viewer gets."""
    import re
    return re.sub(r'<svg.*?</svg>|<p class="upd">.*?</p>', "", html)


def test_login_markets_says_when_the_closes_were_last_fetched(dbfile):
    _prices(dbfile, [(_day(1), "SPX", 6811.6, "y")])
    assert server.login_markets().endswith(f'</div><p class="upd">As of {date.today() - timedelta(days=1):%b} {(date.today() - timedelta(days=1)).day}</p>')
    con = ctdb.connect(dbfile)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('prices_updated', '2026-11-15T19:45:00+00:00')")
    con.commit()
    server._login_markets = None
    import re
    assert re.search(r'</div><p class="upd">Updated Nov 1[56], \d{1,2}:\d\d [AP]M( [A-Z+\-0-9]+)?</p>$', server.login_markets())  # server's local time
    con.execute("INSERT OR REPLACE INTO meta VALUES ('prices_updated', '<b>soon</b>')")
    con.commit()
    con.close()
    server._login_markets = None
    assert "<b>soon" not in server.login_markets() and '<p class="upd">As of ' in server.login_markets()


def test_login_markets_shows_the_latest_close_of_each_symbol_in_order_with_its_daily_move(dbfile):
    _prices(dbfile, [(_day(3), "BTC", 90000.0, "coinbase"), (_day(1), "BTC", 97000.0, "coinbase"), (_day(0), "BTC", 96250.4, "coinbase"),
                     (_day(5), "SPX", 6800.0, "yahoo ^GSPC"), (_day(2), "SPX", 6811.6, "yahoo ^GSPC"),  # over a weekend
                     (_day(1), "XAU", 4105.2, "yahoo GC=F"), (_day(0), "IXIC", 22000.0, "yahoo ^IXIC"), (_day(0), "XAG", 50.0, "yahoo SI=F")])
    html = server.login_markets()
    assert html.count("<svg") == 3 and "<script" not in html and "href" not in html  # inline icons only: the CSP allows nothing else
    assert _mkt(html) == (
        f'<div class="mkt" title="Market closes as of {_day(0)}">'
        f'<div><span class="n">Bitcoin</span><b>$96,250</b><i class="down" title="Since the close before, {_day(1)}">−0.8%</i></div>'
        '<div><span class="n">Gold</span><b>$4,105</b></div>'  # one close stored: no move to show
        f'<div><span class="n">S&amp;P 500</span><b>6,812</b><i class="up" title="Since the close before, {_day(5)}">+0.2%</i></div>'
        '</div>')  # the Nasdaq and silver are stored, not listed


def test_login_markets_move_needs_a_recent_valid_close_before(dbfile):
    gap = server.LOGIN_MARKETS_PREV_GAP
    _prices(dbfile, [(_day(gap + 1), "SPX", 6000.0, "y"), (_day(0), "SPX", 6811.0, "y"),   # the close before is too far back
                     (_day(1), "XAU", "n/a", "y"), (_day(0), "XAU", 4105.0, "y"),         # ... or isn't a price
                     (_day(1), "BTC", 96250.0, "c"), (_day(0), "BTC", 96250.0, "c")])     # unchanged: no sign, no colour
    html = _mkt(server.login_markets())
    assert '<b>6,811</b></div>' in html and '<b>$4,105</b></div>' in html
    assert f'<b>$96,250</b><i class="flat" title="Since the close before, {_day(1)}">0.0%</i>' in html


def test_login_page_never_shows_a_stored_symbol_outside_the_fixed_benchmark_list(dbfile):
    """The sign-in page is public: a symbol the ledger happens to store (a holding, a display unit) must not reach it."""
    assert [m[0] for m in server.LOGIN_MARKETS] == ["BTC", "XAU", "SPX"]
    _prices(dbfile, [(_day(0), "SPX", 6811.6, "y"), (_day(0), "IXIC", 22480.3, "y"), (_day(0), "XAG", 51.5, "y"),
                     (_day(0), "IRR", 0.00000054, "y"), (_day(0), "ZZTOP", 31337.0, "manual"), (_day(0), "ETH", 3410.0, "c")])
    sent = []
    h = server.Handler.__new__(server.Handler)
    h._send = lambda code, body, ctype, headers=(), csp=None, cache=None: sent.append(body.decode())
    h._login_page()
    h._login_page(401, "Wrong username or password.", username="u")
    for page in sent:
        assert _mkt(page).count('<div><span class="n">') == 1 and '<span class="n">S&amp;P 500</span><b>6,812</b>' in _mkt(page)
        for leak in ("ZZTOP", "31,337", "31337", "IXIC", "Nasdaq", "22,480", "XAG", "IRR", "ETH", "3,410"):
            assert leak not in page, leak
    server._login_markets = None  # nothing listed is stored: no line at all, whatever else the table holds
    con = ctdb.connect(dbfile)
    con.execute("DELETE FROM prices WHERE symbol = 'SPX'")
    con.commit()
    con.close()
    sent.clear()
    h._login_page()
    assert 'class="mkt"' not in sent[0] and "ZZTOP" not in sent[0] and "{{markets}}" not in sent[0]


def test_login_markets_leaves_out_stale_missing_and_garbage_rows(dbfile, monkeypatch):
    # SPX a day too old, gold just fresh enough, bitcoin's latest row is not a price
    _prices(dbfile, [(_day(server.LOGIN_MARKETS_MAX_AGE + 1), "SPX", 6800.0, "y"),
                     (_day(server.LOGIN_MARKETS_MAX_AGE), "XAU", 4100.0, "y"),
                     (_day(1), "BTC", 96000.0, "c"), (_day(0), "BTC", "n/a", "c")])
    assert _mkt(server.login_markets()) == (f'<div class="mkt" title="Market closes as of {_day(server.LOGIN_MARKETS_MAX_AGE)}">'
                                            '<div><span class="n">Gold</span><b>$4,100</b></div></div>')
    monkeypatch.setattr(server, "_login_markets", None)
    con = ctdb.connect(dbfile)
    con.execute("DELETE FROM prices")
    con.commit()
    con.close()
    # a non-positive price, a date that isn't one, a date far in the future: nothing left to show
    _prices(dbfile, [(_day(0), "SPX", -5.0, "y"), ("soon", "XAU", 4100.0, "y"), (_day(-30), "BTC", 96000.0, "c")])
    assert server.login_markets() == ""


def test_login_markets_is_empty_without_a_prices_table_a_database_or_on_any_error(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    sqlite3.connect(path).executescript("CREATE TABLE balances (x);")
    monkeypatch.setattr(server, "DB", str(path))
    assert server.login_markets() == ""
    monkeypatch.setattr(server, "_login_markets", None)
    monkeypatch.setattr(server, "DB", str(tmp_path / "missing.db"))
    assert server.login_markets() == "" and not (tmp_path / "missing.db").exists()  # read-only: nothing is created
    monkeypatch.setattr(server, "_login_markets", None)
    logged = []
    monkeypatch.setattr(server, "log", logged.append)
    monkeypatch.setattr(server, "q", lambda *a: 1 / 0)
    assert server.login_markets() == "" and len(logged) == 1 and "ZeroDivisionError" in logged[0]


def test_login_markets_looks_once_per_ttl_and_never_queues_behind_another_lookup(dbfile, monkeypatch):
    clock, calls, build = [1000.0], [], server._market_closes_html
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(server, "_market_closes_html", lambda: calls.append(1) or build())
    with server._login_markets_lock:  # a lookup in progress elsewhere: answer at once, without a second query
        assert server.login_markets() == "" and not calls and server._login_markets is None
    assert server.login_markets() == "" and server.login_markets() == "" and len(calls) == 1  # "" is remembered too
    _prices(dbfile, [(_day(0), "BTC", 96250.0, "coinbase")])
    clock[0] += server.LOGIN_MARKETS_TTL - 1
    assert server.login_markets() == "" and len(calls) == 1
    clock[0] += 2
    assert "Bitcoin</span><b>$96,250</b>" in server.login_markets() and len(calls) == 2
    with server._login_markets_lock:  # expired while another request refreshes it: the last answer, still no query
        clock[0] += server.LOGIN_MARKETS_TTL + 1
        assert "Bitcoin" in server.login_markets() and len(calls) == 2


def test_login_markets_escapes_its_labels(dbfile, monkeypatch):
    _prices(dbfile, [(_day(0), "BTC", 1234567.0, "coinbase")])
    monkeypatch.setattr(server, "LOGIN_MARKETS", (("BTC", '<i>"B"&', "$", ""),))
    assert _mkt(server.login_markets()) == (f'<div class="mkt" title="Market closes as of {_day(0)}"><div><span class="n">'
                                            '&lt;i&gt;&quot;B&quot;&amp;</span><b>$1,234,567</b></div></div>')


def _meta(dbfile, key, value):
    con = ctdb.connect(dbfile)
    con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
    con.commit()
    con.close()


def test_login_markets_adds_the_inflation_nowcast_the_sync_stored(dbfile, monkeypatch):
    tile = lambda: _mkt(server.login_markets())
    _meta(dbfile, "inflation", json.dumps({"pct": 3.94, "asof": _day(2), "month": "2026-11", "source": "Cleveland Fed nowcast"}))
    assert tile() == (f'<div class="mkt" title="Market closes as of {_day(2)}"><div><span class="n">Inflation</span><b>3.9%</b>'
                      f'<i title="Consumer prices, year over year: the Cleveland Fed nowcast as of {_day(2)}">a year</i></div></div>')
    _prices(dbfile, [(_day(0), "BTC", 96250.0, "c")])
    monkeypatch.setattr(server, "_login_markets", None)
    assert tile().index("Bitcoin") < tile().index("Inflation") and tile().count('<span class="n">') == 2  # after the closes
    for bad in ("", "[1]", '"3.9"', json.dumps({"pct": "<b>", "asof": _day(0)}), json.dumps({"pct": 250, "asof": _day(0)}),
                json.dumps({"pct": 3.9, "asof": _day(server.LOGIN_MARKETS_MAX_AGE + 1)}), json.dumps({"pct": 3.9})):
        _meta(dbfile, "inflation", bad)
        monkeypatch.setattr(server, "_login_markets", None)
        assert "Inflation" not in server.login_markets() and "Bitcoin" in server.login_markets(), bad


def test_dashboard_shows_the_sign_in_pages_market_closes_once():
    index = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    assert index.count("{{markets}}") == 1 and '<div class="mktbar">{{markets}}</div>' in index  # filled by login_markets()
    assert index.count('class="card golink" data-go="asset"') == index.count('class="card golink" data-go="liability"') == 1
