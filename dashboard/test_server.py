"""Dashboard API helpers against a small synthetic database (no real data)."""
import json
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

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


def test_side_changes_split_the_30_day_change_into_assets_and_owed(dbfile):
    """Credit and loan accounts are what's owed (paying them down is a negative change in owed); like with like."""
    con = ctdb.connect(dbfile)
    con.execute("INSERT INTO account_kind VALUES ('m', 'loan', NULL)")
    con.commit()
    con.close()
    s = [{"date": "2026-02-01", "synced": 0.0, "accounts": {"c1": -300.0, "m": -100000.0, "chk": 5000.0}},
         {"date": "2026-03-03", "synced": 0.0, "accounts": {"c1": -250.0, "m": -99500.0, "chk": 4200.0, "new": 900.0}}]
    # "new" was linked since then: left out, and said, as the net worth change does
    assert server.side_changes(s) == {"assets": -800.0, "liabilities": -550.0, "vs": "2026-02-01", "excluded": 1, "excluded_total": 900.0}
    assert {k: v for k, v in server.period_changes(s)["30"].items() if k.startswith("excluded")} == {"excluded": 1, "excluded_total": 900.0}
    del s[1]["accounts"]["new"]
    assert "excluded" not in server.side_changes(s)
    assert server.side_changes(s[1:]) is None and server.side_changes([]) is None  # history too short
    assert server.side_changes([{"date": "2026-02-01", "synced": 1.0}, {"date": "2026-03-03", "synced": 2.0}]) is None


def test_summary_carries_the_debt_lines_for_the_liabilities_tile(dbfile, monkeypatch):
    monkeypatch.setattr(server.mortgage, "summary", lambda con, rate_hint="": {"payoff": "2035-09", "hint": rate_hint})
    data = {"lines": [{"line": "Home mortgage 6%", "type": "liability", "used": 1.0},
                      {"line": "Visa", "type": "liability", "used": 1.0}]}
    d = server.debt_summary(data)
    assert d["mortgage"] == {"payoff": "2035-09", "hint": "Home mortgage 6%"}  # only mortgage lines name the rate
    assert d["cards"] == {"owed": 310.0, "limit": 1500.0, "util_pct": 16.7, "count": 3}
    monkeypatch.setattr(server.mortgage, "summary", lambda con, rate_hint="": 1 / 0)
    logged = []
    monkeypatch.setattr(server, "log", logged.append)
    assert server.debt_summary(data)["mortgage"] is None and "ZeroDivisionError" in logged[0]  # never fails the summary


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
    assert all(set(p) <= {"date", "synced", "total", "close"} for p in server.api_trend({})["series"])


def test_synced_series_values_holdings_at_each_days_close(dbfile, monkeypatch):
    """The feed's balance lags the market: a snapshot's holdings are revalued at each later day's close (a weekend
    carries Friday's), money market at $1, the snapshot's non-holdings remainder kept, no-close symbols at the feed's
    price (and named); an account without holdings keeps its balance."""
    con = ctdb.connect(dbfile)
    con.execute("DELETE FROM balances")
    con.execute("INSERT INTO account_kind VALUES ('ira','retirement',NULL), ('s1','savings',NULL)")
    for d in ("2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09"):  # Fri..Mon
        con.execute(ctdb.UPSERT_BALANCE, (d, "simplefin", "s1", "Savings (9)", "Bank", "500.00", None, "USD", None))
    # one IRA snapshot on Friday, at Thursday's prices (the feed is late): 100 AAA @10, 200 money market, 7 ZZZ @10,
    # 30 of cash outside any position; then nothing new from the feed until Monday
    con.execute(ctdb.UPSERT_BALANCE, ("2026-11-06", "simplefin", "ira", "IRA (1)", "Broker", "1300.00", None, "USD", None))
    for sym, sh, px in (("AAA", 100, 10.0), ("SPAXX", 200, 1.0), ("ZZZ", 7, 10.0)):
        con.execute(ctdb.UPSERT_HOLDING, ("2026-11-06", "simplefin", "ira", "IRA (1)", "Broker", sym, sym, sym,
                                          str(sh), str(px), str(sh * px), "0", "USD"))
    con.executemany("INSERT INTO security_prices VALUES (?,?,?,?)", [
        ("2026-11-05", "AAA", 10.0, "y"), ("2026-11-06", "AAA", 11.0, "y"), ("2026-11-09", "AAA", 12.0, "y")])
    con.commit()
    con.close()
    series = server.synced_series()
    ira = {p["date"]: p["accounts"]["ira"] for p in series}
    assert ira == {"2026-11-06": pytest.approx(1100 + 200 + 70 + 30), "2026-11-07": pytest.approx(1400.0),
                   "2026-11-08": pytest.approx(1400.0), "2026-11-09": pytest.approx(1200 + 200 + 70 + 30)}
    assert [p["accounts"]["s1"] for p in series] == [500.0] * 4
    assert [p.get("close") for p in series] == ["2026-11-06", "2026-11-06", "2026-11-06", "2026-11-09"]
    assert series.fallback == {"ZZZ": 4} and series.repriced == 1
    monkeypatch.setattr(server, "networth", lambda refresh=False: {"data": None, "error": "x"})
    out = server.api_trend({})
    assert out["series"][1] == {"date": "2026-11-07", "synced": 1900.0, "close": "2026-11-06", "total": None}
    assert out["valued"] == {"accounts": 1, "fallback": {"ZZZ": 4}}


def test_benchmark_lag_says_which_days_close_each_date_carries(dbfile, monkeypatch):
    monkeypatch.setattr(server, "networth", lambda refresh=False: {"data": None, "error": "x"})
    con = ctdb.connect(dbfile)
    con.executemany("INSERT INTO prices VALUES (?,?,?,?)", [
        ("2026-11-06", "SPX", 6000.0, "y"), ("2026-11-09", "SPX", 6100.0, "y")])  # Fri, Mon
    con.commit()
    con.close()
    b = server.trend_prices(["2026-11-06"], today="2026-11-09")["benchmarks"]
    assert b["dates"] == ["2026-11-06", "2026-11-07", "2026-11-08", "2026-11-09"]
    assert b["prices"]["SPX"] == [6000.0, 6000.0, 6000.0, 6100.0] and b["lag"]["SPX"] == [0, 1, 2, 0]
    assert b["lag"]["IXIC"] == [None] * 4


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


def test_holdings_value_exchange_coins_at_the_daily_runs_price(dbfile):
    """A synced crypto exchange sends coins with no price or value: they are valued at the latest crypto run's price
    (the same daily price the Crypto card uses), not left out at $0; the exchange's dollar cash is not a holding."""
    con = ctdb.connect(dbfile)
    for aid, kind in (("ira", "retirement"), ("cb", "crypto")):
        con.execute("INSERT INTO account_kind VALUES (?,?,NULL)", (aid, kind))
    con.executemany("INSERT INTO holdings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (TODAY, "simplefin", "ira", "IRA (1234)", "Fid", "k1", "BTC", "Bitcoin Mini Trust", "100", "40", "4000", None, "USD"),
        (TODAY, "simplefin", "cb", "Wallet (ab-12)", "CB", "k2", "BTC", "Bitcoin", "0.1", "0", "0", None, "USD"),
        (TODAY, "simplefin", "cb", "Wallet (ab-12)", "CB", "k3", "ETH2", "Staked ether", "2", None, "0", None, "USD"),
        (TODAY, "simplefin", "cb", "Wallet (ab-12)", "CB", "k4", "USD", "Cash", "500", None, "0", None, "USD"),
        (TODAY, "simplefin", "cb", "Wallet (ab-12)", "CB", "k5", "ZZZ", "Unpriced", "3", None, "0", None, "USD")])
    con.commit()
    con.close()
    assert [h["symbol"] for h in server.api_holdings({})["holdings"]] == ["BTC"]  # no crypto run yet: as the feed says
    con = ctdb.connect(dbfile)
    con.execute("INSERT INTO valuations VALUES (?,?,?,?,?,?)", (TODAY, server.CRYPTO_LINE, 1.0, "crypto", json.dumps(
        {"by_coin": {"BTC": {"qty": 1, "price": 80000.0, "value": 80000.0}, "ETH": {"qty": 1, "price": 3000.0, "value": 3000.0},
                     "USD": {"qty": 1, "price": 1.0, "value": 1.0}}}), "2026-01-01T00:00:00"))
    con.commit()
    con.close()
    out = server.api_holdings({})
    assert [(h["symbol"], h["value"], h["accounts"]) for h in out["holdings"]] == [
        ("BTC", 12000.0, ["IRA", "Wallet"]), ("ETH2", 6000.0, ["Wallet"])]
    assert out["total"] == 18000.0


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
    assert server.read_status(now)["problems"][0].startswith("No sync has run yet")


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
    for needle in ('<div id="loadbar" role="progressbar" aria-label="Loading"></div>', '$("loadbar").hidden = !list.length;',
                   "const TIMEOUT_MS = 20e3, SLOW_MS = 75e3", "signal: ac.signal", "setTimeout(() => ac.abort(), timeout)",
                   "no answer after ${Math.round(timeout / 1000)} seconds", "can't reach the server",
                   'id="nw"><span class="skel" role="status" aria-label="Loading"></span>',
                   'id="assets"><span class="skel"', 'id="liabs"><span class="skel"',
                   '<span class="pill"><span class="spin" aria-hidden="true"></span>Loading…</span>',
                   "Couldn't load this section.", "Couldn't refresh this section. Showing the numbers from ${esc(fmtFrom(shownAt[k]))}.",
                   '<button type="button" data-retry="${k}">Retry</button>', 'e.target.closest("button[data-retry]")',
                   'setLoad(k, k in data ? "stale" : "error", e.message)', 'id="acctretry"',
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
    for slow in ("summary", "trend", "spending", "budget"):  # these wait on a sheet read, which the server allows 60 s
        assert re.search(rf"\b{slow}: \{{[^}}]*slow: true\}}", parts), slow


def test_page_shows_the_last_numbers_first_and_says_what_it_is_waiting_on():
    """Guard on the page source (behaviour is checked in a headless browser): the first load draws the server's last
    good answers, at full strength (never dimmed) until fresh ones land; each request in flight is named in plain words with its seconds so
    far, its usual time and its time limit; a failed refresh keeps the numbers and says from when. Only how long
    requests take is kept in the browser, never an answer."""
    import re
    html = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    for needle in ('<div id="loadlist" hidden></div>', 'await get("/api/last", {timeout: 8e3})', "const last = everLoaded ? null : showLast();",
                   'setLoad(k, k in data ? "old" : "loading")', 'setLoad(k, failed.has(k) ? "stale" : pending[k] ? "old" : null, failed.get(k))',
                   'const TIMES_KEY = "wl.loadtimes", TIMES_KEPT = 8;', "localStorage.setItem(TIMES_KEY, JSON.stringify(loadTimes))",
                   '"usually " + secsText(u) + " · "', "gives up at ${Math.round(f.timeout / 1000)}s", "slower than usual",
                   "ms > Math.max(2 * u, 2000)", 'Math.floor(ms / 1000) + "s"', 'title="${esc(f.method + " " + f.path)}"',
                   "showing the numbers from ${fmtFrom(from)} meanwhile", "`+${more} more`", "Couldn't refresh · showing numbers from ${esc(fmtFrom(shownAt.summary))}",
                   "Showing the numbers from ${esc(fmtFrom(Math.min(...parts.map(k => shownAt[k]))))}."):
        assert needle in html, needle
    assert not re.search(r'\[data-load="old"\][^{]*\{[^}]*(opacity|filter)', html), "earlier numbers must not be dimmed while reloading"
    names = html[html.index("const REQ_NAME = {"):html.index("// How long each request usually takes")]
    named = set(re.findall(r"(\w+): \"", names))
    parts = html[html.index("const PARTS = {"):html.index("const SKEL =")]
    routes = re.findall(r'url: "/api/(\w+)"', parts)
    assert sorted(routes) == sorted(server.LAST_KEYS)  # every request the page starts with has a kept answer
    assert named == set(routes) | {"refresh", "account_txns", "last", "sync", "syncrun"} and "/api/" not in names  # words, not addresses
    stored = set(re.findall(r"localStorage\.setItem\((\w+)", html))
    assert stored == {"ARANGE_KEY", "BENCH_KEY", "ORDER_KEY", "PRIV_KEY", "SEC_KEY", "THEME_KEY", "TIMES_KEY", "TREND_KEY", "UNIT_KEY"}
    assert "sessionStorage" not in html and "indexedDB" not in html and "caches." not in html


def test_page_script_parses(tmp_path):
    """A name declared twice or a stray brace stops the whole page: every card would sit on its placeholder."""
    import re, shutil, subprocess
    if not shutil.which("node"):
        pytest.skip("node not installed")
    html = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == 2
    for i, js in enumerate(scripts):
        f = tmp_path / f"page{i}.js"
        f.write_text(js)
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-600:]


@pytest.fixture
def lastdir(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "LAST_DIR", str(tmp_path / "conf" / "dashboard_last"))
    monkeypatch.setattr(server, "_last", None)
    monkeypatch.setattr(server, "_nw", {"data": {"lines": []}, "fetched": 1.0, "error": None})
    return tmp_path / "conf" / "dashboard_last"


def test_last_good_answers_are_kept_private_on_disk_and_survive_a_restart(lastdir, monkeypatch):
    assert server.last_good() == b"{}" and not lastdir.exists()  # nothing kept yet, nothing created by asking
    body = json.dumps({"accounts": [{"balance": 12.5}]}).encode()
    monkeypatch.setattr(server.time, "time", lambda: 1000.0)
    server.remember("accounts", json.loads(body), body)
    server.remember("account_txns", {"txns": []}, b'{"txns": []}')  # one account's rows: not a page-load request
    server.remember("../evil", {}, b"{}")
    assert [p.name for p in lastdir.iterdir()] == ["accounts.json"] and (lastdir / "accounts.json").read_bytes() == body
    assert (lastdir / "accounts.json").stat().st_mode & 0o777 == 0o600 and lastdir.stat().st_mode & 0o777 == 0o700
    assert json.loads(server.last_good()) == {"accounts": {"saved": 1000.0, "data": {"accounts": [{"balance": 12.5}]}}}
    ino = (lastdir / "accounts.json").stat().st_ino
    monkeypatch.setattr(server.time, "time", lambda: 2000.0)
    server.remember("accounts", json.loads(body), body)  # the same answer again: only its time moves
    assert (lastdir / "accounts.json").stat().st_ino == ino and (lastdir / "accounts.json").stat().st_mtime == 2000.0
    assert json.loads(server.last_good())["accounts"]["saved"] == 2000.0
    (lastdir / "cards.json").write_text('{"cards": [')  # torn file
    (lastdir / "rental.json").write_text("[1]")           # not an answer
    monkeypatch.setattr(server, "_last", None)            # a restart: back from disk
    assert json.loads(server.last_good()) == {"accounts": {"saved": 2000.0, "data": {"accounts": [{"balance": 12.5}]}}}
    monkeypatch.setattr(server, "LAST_DIR", "/proc/nope/dashboard_last")  # can't write: still answered from memory
    server.remember("holdings", {"count": 1}, b'{"count": 1}')
    assert json.loads(server.last_good())["holdings"]["data"] == {"count": 1}


def test_an_answer_with_nothing_to_show_does_not_replace_the_last_good_one(lastdir, monkeypatch):
    good = b'{"net_worth": 5.0}'
    server.remember("summary", {"net_worth": 5.0}, good)
    server.remember("budget", {"error": None, "months": []}, b'{"months": []}')
    monkeypatch.setattr(server, "_nw", {"data": None, "fetched": 0.0, "error": "networth.py failed: boom"})
    server.remember("summary", {"net_worth": None, "networth_error": "networth.py failed: boom"}, b'{"net_worth": null}')
    server.remember("trend", {"sheet_only_total": None}, b'{"sheet_only_total": null}')
    server.remember("budget", {"error": "budget sheet read failed: boom"}, b'{"error": "boom"}')
    kept = json.loads(server.last_good())
    assert kept["summary"]["data"] == {"net_worth": 5.0} and kept["budget"]["data"] == {"months": []} and "trend" not in kept
    monkeypatch.setattr(server, "_nw", {"data": None, "fetched": 0.0, "error": None})  # a ledger with nothing yet is still an answer
    server.remember("trend", {"sheet_only_total": None}, b'{"sheet_only_total": null}')
    server.remember("budget", {"error": None, "off": True}, b'{"off": true}')
    assert set(json.loads(server.last_good())) == {"summary", "budget", "trend"} and json.loads(server.last_good())["budget"]["data"] == {"off": True}


def test_requests_keep_their_answers_and_api_last_needs_a_session(lastdir, monkeypatch):
    sent = []
    h = server.Handler.__new__(server.Handler)
    h._send = lambda code, body, ctype, headers=(), csp=None, cache=None: sent.append((code, body, ctype))
    h._deny = lambda p: sent.append((401, p))
    h._authed = lambda: False
    h.path = "/api/last"
    h.do_GET()
    assert sent == [(401, "/api/last")] and not lastdir.exists()
    h._authed = lambda: True
    monkeypatch.setitem(server.ROUTES, "/api/cards", lambda params: {"cards": [1], "id": params.get("id")})
    monkeypatch.setitem(server.ROUTES, "/api/rental", lambda params: 1 / 0)
    for path in ("/api/cards", "/api/cards?id=7", "/api/rental", "/api/last"):
        h.path = path
        h.do_GET()
    assert [c for c, *_ in sent[1:]] == [200, 200, 500, 200] and sent[1][1] == b'{"cards": [1], "id": null}'
    kept = json.loads(sent[-1][1])
    assert sent[-1][2] == "application/json" and list(kept) == ["cards"]  # not the filtered answer, not the failed one
    assert kept["cards"]["data"] == {"cards": [1], "id": None} and kept["cards"]["saved"] > 0


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


def test_page_shows_an_interpolated_correlation_like_a_measured_one():
    """Guard on the page source: a range the daily snapshots don't cover still gets a figure, net worth interpolated in a
    straight line between snapshots, shown plainly: no "≈", no "est" tag or style, no note, no "estimate" wording. The
    first daily date comes from the data."""
    import re
    html = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    for needle in ("function benchCorrEst(t, u, pts, k)", "const path = lead ? [lead, ...real] : real",
                   'd0 = pts.find(p => p.kind === "daily")', 'el.textContent = !c ? "" : !num ? "—" : num;',
                   "net worth is interpolated between balance-sheet snapshots", "if (n < 2) return null;"):
        assert needle in html, needle
    assert "needs 8" not in html and '"few"' not in html  # a short range of daily snapshots is measured too
    js = html[html.index("function benchCorr("):html.index("const pctStr")]
    for gone in ("≈", "<small>est", 'toggle("est"', "trendcorrnote", "estimate"):
        assert gone not in js, gone
    assert ".corr.est" not in html and "trendcorrnote" not in html
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


def test_disputed_charges_count_nowhere(dbfile, monkeypatch):
    """A charge reversed by a won dispute (txn_class_overrides class 'disputed', its credit never in the feed) is
    not spending, not money out, and leaves the walked-back balance alone."""
    class Today(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 13)
    monkeypatch.setattr(server, "date", Today)
    monkeypatch.setattr(server, "LEDGER_START", "2026-09-01")
    _txns(dbfile, [("2026-10-02", "-40.00", "GROCER", 0), ("2026-10-03", "-900.00", "ONLINE STORE", 0),
                   ("2026-10-04", "-60.00", "GROCER", 0)])
    con = ctdb.connect(dbfile)
    con.execute(ctdb.UPSERT_BALANCE, (TODAY, "simplefin", "k1", "Test k1 (1234)", "Bank", "1000.00", None, "USD", None))
    con.execute("INSERT INTO txn_class_overrides (txn_id, class) VALUES ('s1', 'disputed')")
    con.commit()
    con.close()
    sp = server.api_spending({})
    assert {m["month"]: m["spent"] for m in sp["monthly"]} == {"2026-10": 100}
    assert [t["merchant"] for t in sp["top"]["2026-10"]] == ["Grocer"]
    row = next(t for t in sp["recent"] if t["description"] == "ONLINE STORE")
    assert row["class"] == "disputed" and row["category"] is None
    r = server.api_account_txns({"id": ["k1"]})
    assert r["money_out"] == -100
    assert {t["description"] + t["date"][-2:]: t["balance_after"] for t in r["txns"]} == {
        "GROCER04": 1000, "ONLINE STORE03": 1060, "GROCER02": 1060}


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
    monkeypatch.setattr(server, "STATUS", str(tmp_path / "status"))
    monkeypatch.setattr(server, "SYNC_LOCK", str(tmp_path / "lock"))
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


# ---------- Sync now ----------
class _FakeSync:
    """Stands in for the sync process: nothing is ever run."""
    def __init__(self, rc=0):
        self.rc = rc

    def wait(self, timeout=None):
        return self.rc


@pytest.fixture
def sync(tmp_path, monkeypatch):
    """Every path Sync now touches in a temp folder, the process start replaced, the watcher thread not started."""
    import threading
    spawned, threads = [], []
    (tmp_path / "access").write_text("x")
    monkeypatch.setattr(server, "ACCESS_FILE", str(tmp_path / "access"))
    monkeypatch.setattr(server, "STATUS", str(tmp_path / "status"))
    monkeypatch.setattr(server, "SYNC_LOCK", str(tmp_path / "lock"))
    monkeypatch.setattr(server, "SYNC_LOG", str(tmp_path / "logs" / "sync.log"))
    monkeypatch.setattr(server, "SYNC_STATE_FILE", str(tmp_path / "conf" / "dashboard_sync.json"))
    monkeypatch.setattr(server, "SYNC_CMD", [str(tmp_path / "never-run")])
    monkeypatch.setattr(server, "WEB_CONNECT", False)
    monkeypatch.setattr(server, "_sync", {"proc": None, "started": None, "last": None})
    monkeypatch.setattr(server, "_spawn_sync", lambda: spawned.append(_FakeSync()) or spawned[-1])
    monkeypatch.setattr(threading.Thread, "start", lambda self: threads.append(self))
    return spawned, threads


def _status(tmp_path, minutes_ago: float, rc: int = 0, now: float | None = None):
    from datetime import datetime
    t = datetime.fromtimestamp((now or server.time.time()) - minutes_ago * 60).astimezone().isoformat(timespec="seconds")
    (tmp_path / "status").write_text(f"last run {t} rc={rc}\n")


def test_sync_now_runs_the_instances_own_sync_command():
    default = server.sync_command({})
    assert default == [server.os.path.join(server.ROOT, "bin", "wealth-ledger-sync")] and server.os.access(default[0], server.os.X_OK)
    assert server.sync_command({"WEALTH_LEDGER_SYNC_CMD": "/opt/my sync/run --days 3"}) == ["/opt/my", "sync/run", "--days", "3"]
    assert server.sync_command({"WEALTH_LEDGER_SYNC_CMD": "'/opt/my sync/run' --days 3"}) == ["/opt/my sync/run", "--days", "3"]
    assert server.sync_command({"WEALTH_LEDGER_SYNC_CMD": ""}) == default
    assert (server.SYNC_MIN_GAP_S, server.SYNC_MAX_PER_DAY) == (900, 8)


def test_sync_now_starts_one_sync_in_the_background_and_reports_how_it_ended(sync, tmp_path, monkeypatch):
    spawned, threads = sync
    monkeypatch.setattr(server, "_nw", {"data": {"x": 1}, "fetched": 123.0, "error": None})
    assert server.sync_state()["running"] is False and server.sync_state()["last"] is None
    assert server.start_sync() == (200, {"started": True, "running": True, "message": ""})
    assert len(spawned) == 1 and len(threads) == 1  # started, and a thread waits for it: the request doesn't
    saved = tmp_path / "conf" / "dashboard_sync.json"
    assert len(json.loads(saved.read_text())["starts"]) == 1 and saved.stat().st_mode & 0o777 == 0o600
    st = server.sync_state()
    assert st["running"] is True and 0 <= st["elapsed_s"] < 5 and st["last"] is None
    # a second click while it runs starts nothing and is not counted
    assert server.start_sync() == (200, {"started": False, "running": True, "message": "A sync is already running"})
    assert len(spawned) == 1 and len(json.loads(saved.read_text())["starts"]) == 1
    spawned[0].rc = 3
    threads[0].run()  # the sync ends
    st = server.sync_state()
    assert st["running"] is False and st["elapsed_s"] is None
    assert st["last"]["rc"] == 3 and st["last"]["error"] is None and st["last"]["finished"] >= st["last"]["started"]
    assert server._nw["fetched"] == 0.0  # the next summary recomputes the net worth


def test_sync_now_waits_15_minutes_after_any_sync(sync, tmp_path):
    spawned, _ = sync
    _status(tmp_path, 5)  # the cron run finished 5 minutes ago
    code, body = server.start_sync()
    assert code == 429 and body["message"] == "Synced 5 min ago — try again in 10 min" and body["started"] is False
    assert 9 * 60 < body["wait_s"] <= 10 * 60 and not spawned and not (tmp_path / "conf").exists()
    assert server.sync_state()["message"] == body["message"] and server.sync_state()["running"] is False
    _status(tmp_path, 5, rc=1)  # a failed run counts too, and isn't called "synced"
    assert server.start_sync()[1]["message"] == "Last sync ran 5 min ago — try again in 10 min"
    _status(tmp_path, 16)
    assert server.start_sync()[0] == 200 and len(spawned) == 1
    server._sync.update(proc=None, started=None)  # it ended without rewriting the status file
    code, body = server.start_sync()
    assert code == 429 and body["message"] == "Last sync ran less than a minute ago — try again in 15 min" and len(spawned) == 1
    now = server.time.time()
    assert server.start_sync(now + 14 * 60)[0] == 429
    assert server.start_sync(now + 16 * 60)[0] == 200 and len(spawned) == 2


def test_sync_now_is_capped_per_day_and_the_count_survives_a_restart(sync, tmp_path):
    spawned, _ = sync
    now = 1_800_000_000.0
    saved = tmp_path / "conf" / "dashboard_sync.json"
    saved.parent.mkdir()
    # 8 clicks in the last 24 hours (the oldest 23 h ago, the newest 2 h ago) and one from two days back
    starts = [now - 2 * 86400] + [now - 3600 * h for h in (23, 20, 17, 14, 11, 8, 5, 2)]
    saved.write_text(json.dumps({"starts": starts}))
    code, body = server.start_sync(now)
    assert code == 429 and not spawned
    assert body["message"] == "Sync now has run 8 times in the last 24 hours, its daily limit — try again in 1 h 00 min"
    assert server.sync_state(now)["wait_s"] == 3600
    assert server.start_sync(now + 3601)[0] == 200 and len(spawned) == 1  # the oldest click is now over a day old
    assert len(json.loads(saved.read_text())["starts"]) == 8  # and the two-day-old one is dropped
    saved.write_text("not json")  # an unreadable count never blocks the button
    server._sync.update(proc=None, started=None)
    assert server.start_sync(now + 3 * 3600)[0] == 200


def test_sync_now_follows_a_sync_that_is_already_running(sync, tmp_path):
    """The sync script holds an exclusive flock while it runs (cron's run, or another dashboard's)."""
    import fcntl
    spawned, _ = sync
    assert server.sync_lock_held() is False  # no lock file yet
    with open(tmp_path / "lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert server.sync_lock_held() is True
        assert server.start_sync() == (200, {"started": False, "running": True, "message": "A sync is already running"})
        st = server.sync_state()
        assert st["running"] is True and st["elapsed_s"] is None  # not started here: no start time to count from
    assert not spawned and not (tmp_path / "conf").exists()  # not counted against the daily limit
    assert server.sync_lock_held() is False and server.sync_state()["running"] is False
    assert server.sync_lock_held() is False  # the check itself leaves nothing locked
    assert server.start_sync()[0] == 200 and len(spawned) == 1


def test_sync_now_says_when_the_sync_cannot_start_or_nothing_is_connected(sync, tmp_path, monkeypatch):
    spawned, threads = sync

    def boom():
        raise FileNotFoundError(2, "No such file or directory")
    monkeypatch.setattr(server, "_spawn_sync", boom)
    code, body = server.start_sync()
    assert code == 500 and body == {"started": False, "running": False,
                                    "message": "The sync could not be started (No such file or directory)"}
    st = server.sync_state()
    assert st["running"] is False and st["last"]["rc"] is None and st["last"]["error"] == body["message"]
    assert not threads and not (tmp_path / "conf").exists() and st["wait_s"] == 0  # not counted: try again at once
    (tmp_path / "access").unlink()
    code, body = server.start_sync()
    assert code == 409 and body["message"].startswith("No bank connection yet") and not spawned


def test_sync_now_without_systemd_is_a_detached_child_that_appends_to_the_sync_log(sync, tmp_path, monkeypatch):
    """The fallback start for real, with a stand-in command (never the sync script): own session, output appended
    to the log (its folder made on the way)."""
    monkeypatch.undo()
    log = tmp_path / "logs" / "sync.log"
    monkeypatch.setattr(server, "SYNC_LOG", str(log))
    monkeypatch.setattr(server, "SYNC_CMD", ["sh", "-c", "echo out; echo err >&2; ps -o sid= -p $$"])
    monkeypatch.setattr(server.shutil, "which", lambda name: None)  # no systemd-run on this machine
    assert server._spawn_in_own_unit(server.SYNC_CMD) is None
    log.parent.mkdir()
    log.write_text("earlier run\n")
    proc = server._spawn_sync()
    assert proc.wait(timeout=10) == 0
    lines = log.read_text().split()
    assert lines[:4] == ["earlier", "run", "out", "err"] and int(lines[4]) == proc.pid != server.os.getsid(0)
    # the first sync after connecting gives its own command (the same script with --days 90)
    assert server._spawn_sync(["sh", "-c", "echo first $0 $1", "--days", "90"], max_s=1800).wait(timeout=10) == 0
    assert log.read_text().splitlines()[-1] == "first --days 90"


class _Ran:
    def __init__(self, returncode, stderr=""):
        self.returncode, self.stderr = returncode, stderr


def test_sync_now_runs_as_a_unit_of_its_own_so_a_dashboard_restart_does_not_stop_it(sync, tmp_path, monkeypatch):
    """systemd-run is never really called here: the command lines it would get are checked."""
    monkeypatch.undo()
    checks, started, fallback = [], [], []
    monkeypatch.setattr(server, "SYNC_LOG", str(tmp_path / "logs" / "sync.log"))
    monkeypatch.setattr(server, "SYNC_CMD", ["/opt/wl/sync", "--days", "3"])
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(server.os, "environ", {"WEALTH_LEDGER_DB": "/secret/place/t.db", "PATH": "/usr/bin", "HOME": "/home/u",
                                               "INVOCATION_ID": "abc", "JOURNAL_STREAM": "8:1", "LISTEN_FDS": "1",
                                               "BASH_FUNC_x%%": "() { :; }", "ODD": "a\x1bb"})
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: checks.append((args, kw)) or _Ran(0))
    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kw: started.append((args, kw)) or _FakeSync())
    monkeypatch.setattr(server, "_spawn_detached", lambda cmd: fallback.append(cmd) or _FakeSync())
    assert isinstance(server._spawn_sync(), _FakeSync) and not fallback
    (check, ckw), (args, kw) = checks[0], started[0]
    assert len(checks) == len(started) == 1 and ckw["timeout"] == 10
    unit = next(a for a in args if a.startswith("--unit="))
    assert args[:5] == ["systemd-run", "--user", "--collect", "--wait", "--quiet"] and unit.startswith("--unit=wealth-ledger-sync-btn-")
    assert args[args.index("--") + 1:] == ["/opt/wl/sync", "--days", "3"] and kw["start_new_session"] is True
    log = str(tmp_path / "logs" / "sync.log")
    assert f"--property=StandardOutput=append:{log}" in args and f"--property=StandardError=append:{log}" in args
    # the dashboard's environment goes along by name only (no value on a command line); systemd's own per-unit
    # variables and names or values systemd would refuse are left out
    assert [a for a in args if a.startswith("--setenv=")] == ["--setenv=HOME", "--setenv=PATH", "--setenv=WEALTH_LEDGER_DB"]
    assert not any("secret" in a for a in args)
    # the check before it: the same unit settings around a command that does nothing
    assert check[check.index("--") + 1:] == ["true"] and check[:check.index("--")] == [a + "-check" if a == unit else a for a in args[:args.index("--")]]
    assert not any("RuntimeMaxSec" in a for a in args)  # Sync now has no time limit, like the cron run
    # the first sync after connecting: its own command, and systemd stops it at the limit (the check unit agrees)
    server._spawn_sync(["/opt/wl/sync", "--days", "90"], max_s=1800)
    assert len(started) == 2 and not fallback and started[1][0][started[1][0].index("--") + 1:] == ["/opt/wl/sync", "--days", "90"]
    assert "--property=RuntimeMaxSec=1800" in started[1][0] and "--property=RuntimeMaxSec=1800" in checks[1][0]


def test_sync_now_falls_back_to_a_detached_child_when_systemd_run_cannot_start_a_unit(sync, tmp_path, monkeypatch):
    monkeypatch.undo()
    started, fallback, logged = [], [], []
    monkeypatch.setattr(server, "SYNC_LOG", str(tmp_path / "logs" / "sync.log"))
    monkeypatch.setattr(server, "SYNC_CMD", ["/opt/wl/sync"])
    monkeypatch.setattr(server, "log", logged.append)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kw: started.append(args) or _FakeSync())
    monkeypatch.setattr(server, "_spawn_detached", lambda cmd: fallback.append(1) or _FakeSync())
    # no user manager to talk to (a container, a plain cron or ssh environment)
    monkeypatch.setattr(server.subprocess, "run", lambda args, **kw: _Ran(1, "Failed to connect to bus: No medium found\n"))
    assert isinstance(server._spawn_sync(), _FakeSync)
    assert fallback == [1] and not started and "Failed to connect to bus: No medium found" in logged[0]

    def hangs(args, **kw):
        raise server.subprocess.TimeoutExpired(args, kw["timeout"])
    monkeypatch.setattr(server.subprocess, "run", hangs)
    assert isinstance(server._spawn_sync(), _FakeSync) and fallback == [1, 1] and not started

    def gone(args, **kw):
        raise FileNotFoundError(2, "No such file or directory")
    monkeypatch.setattr(server.subprocess, "run", gone)
    assert isinstance(server._spawn_sync(), _FakeSync) and fallback == [1, 1, 1] and not started


def test_sync_endpoint_needs_a_session_is_post_only_and_refuses_cross_site(sync):
    spawned, _ = sync
    sent = []
    h = server.Handler.__new__(server.Handler)
    h._json = lambda code, obj, headers=(): sent.append((code, obj))
    h._deny = lambda p: sent.append((401, p))
    h.client_address = ("127.0.0.1", 1)
    h.path = "/api/sync"
    h.headers, h._authed = {}, lambda: False
    h.do_GET(); h.do_POST()
    assert sent == [(401, "/api/sync"), (401, "/api/sync")] and not spawned
    h._authed = lambda: True
    h.headers = {"Sec-Fetch-Site": "cross-site"}
    h.do_POST()
    assert sent[-1] == (403, {"error": "cross-site request refused"}) and not spawned
    h.headers = {"Sec-Fetch-Site": "same-origin"}
    h.do_GET()  # asking how it is going never starts one
    assert sent[-1][0] == 200 and sent[-1][1]["running"] is False and sent[-1][1]["wait_s"] == 0 and not spawned
    h.do_POST()
    assert sent[-1] == (200, {"started": True, "running": True, "message": ""}) and len(spawned) == 1
    h.do_GET()
    assert sent[-1][1]["running"] is True and len(spawned) == 1


def test_page_has_sync_now_that_follows_the_sync_and_reloads():
    """Guard on the page source (behaviour is checked in a browser by hand): Sync now posts, follows the sync with the
    same wait line as any request, says why when refused, and reloads when it ends; Refresh has a name of its own."""
    html = open(server.os.path.join(server.os.path.dirname(server.__file__), "index.html")).read()
    for needle in ('<button id="syncnow"', '>Sync now</button>', 'fetch("/api/sync", {method: "POST"', 'await get("/api/sync")',
                   'key: "syncrun"', 'noteTime("syncrun", took)', "`Syncing…<span class=\"hide-sm\"> ${Math.floor((now - syncRun.t0) / 1000)}s</span>`",
                   "`<span class=\"hint\">(usually ${secsText(u)})</span>`", "await load(true);", 'id="syncnote" role="status"',
                   "limited = r.status === 429", "st.finished_ago_s * 1000 <= took + 5000", "SHEETLESS ? \"Reload\" : 'Re-read<span class=\"hide-sm\"> sheet</span>'", "\nif (!DEMO) checkSync();\n",
                   "#status .pill, #syncnow) { display: none; }"):
        assert needle in html, needle
    assert ">Refresh</button>" not in html


def test_first_sync_after_connecting_starts_like_sync_now_and_reports_how_it_ended(connect, monkeypatch):
    """The launcher is replaced: no sync runs. 90 days of history, stopped if it runs past the limit."""
    calls = []

    class Sync:
        def __init__(self, rc=0, hangs=False):
            self.rc, self.hangs, self.killed = rc, hangs, False

        def wait(self, timeout=None):
            if self.hangs:
                raise server.subprocess.TimeoutExpired("sync", timeout)
            return self.rc

        def kill(self):
            self.killed = True
    procs = [Sync(0), Sync(1), Sync(hangs=True)]
    monkeypatch.setattr(server, "_spawn_sync", lambda cmd=None, max_s=None: calls.append((cmd, max_s)) or procs[len(calls) - 1])
    monkeypatch.setattr(server, "_nw", {"data": {"x": 1}, "fetched": 123.0, "error": None})
    server._conn.update(running=True, error=None)
    server._first_sync()
    assert calls == [([server.SYNC, "--days", "90"], server.FIRST_SYNC_MAX_S)] and server.FIRST_SYNC_MAX_S == 1800
    assert server._conn == {"running": False, "error": None} and server._nw["fetched"] == 0.0
    server._conn.update(running=True)
    server._first_sync()
    assert server._conn == {"running": False, "error": "The first sync had problems; see the warnings above."}
    server._conn.update(running=True)
    server._first_sync()  # still going after the limit: the waiting stops and says so
    assert procs[2].killed and server._conn["running"] is False and server._conn["error"].startswith("The first sync did not finish")

    def boom(cmd=None, max_s=None):
        raise FileNotFoundError(2, "No such file or directory")
    monkeypatch.setattr(server, "_spawn_sync", boom)
    server._conn.update(running=True, error=None)
    server._first_sync()
    assert server._conn["running"] is False and server._conn["error"].startswith("The first sync did not finish")


def test_first_sync_still_shows_as_running_after_the_dashboard_restarts(connect, tmp_path):
    """A restarted dashboard has forgotten the first sync it started, but that sync carries on in its own unit:
    connected, no sync finished yet, and the sync's lock held."""
    import fcntl
    assert server.connect_state() == {"needed": True, "running": False, "error": None}
    (tmp_path / "simplefin_access_url").write_text("x")
    assert server.connect_state()["running"] is False  # no lock file: nothing is running
    with open(tmp_path / "lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert server.connect_state() == {"needed": False, "running": True, "error": None}
        (tmp_path / "status").write_text("last run 2026-11-11T08:15:02-06:00 rc=0\n")
        assert server.connect_state()["running"] is False  # a sync has finished before: this is an ordinary one
        (tmp_path / "status").unlink()
    assert server.connect_state()["running"] is False


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
                   'autocomplete="username"', 'autocomplete="current-password"', "{{error}}", "{{username}}",
                   '<div class="mktbar" id="mkts" data-poll="{{markets_poll}}">{{markets}}</div>'):
        assert needle in html, needle
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == html.count("<script") == 2  # theme (in <head>) and form behaviour; nothing loaded from elsewhere
    for body in scripts:
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in server.CSP_LOGIN.split("script-src ")[1].split(";")[0]
    assert "default-src 'none'" in server.CSP_LOGIN and "http" not in server.CSP_LOGIN
    assert "connect-src 'self'" in server.CSP_LOGIN  # the markets line's re-reads, to this server only


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
    assert "Wealth Ledger" in plain and "{{credit}}" not in plain and 'class="by"' not in plain  # no credit by default


def test_credit_line_is_off_unless_configured(monkeypatch):
    assert server.credit_line() == ""
    monkeypatch.setattr(server.personal, "section", lambda name: {"credit": " Built by <Example> "})
    assert server.credit_line() == '<p class="by">Built by &lt;Example&gt;</p>'
    monkeypatch.setattr(server.personal, "section", lambda name: {"credit": ["not text"]})
    assert server.credit_line() == ""


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
    monkeypatch.setattr(server, "_login_markets", None)  # the memos must not carry from one test into the next
    monkeypatch.setattr(server, "_markets_line", None)


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
    return re.sub(r'<svg.*?</svg>|<p class="upd"[^>]*>.*?</p>', "", html)


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
    # filled by markets_line(): the sign-in page's line, live prices included
    assert index.count("{{markets}}") == 1 and '<div class="mktbar" data-poll="{{markets_poll}}">{{markets}}</div>' in index
    assert index.count('class="card golink tile tile-a" data-go="asset"') == index.count('class="card golink tile tile-l" data-go="liability"') == 1


# ---------- live prices in the dashboard's markets line ----------
def _live(sym, usd, day, vs="same", source="coinbase", t=None):
    import time
    t = t or time.time()
    return {sym: {"usd": usd, "vs": usd if vs == "same" else vs, "day": day, "time": t, "at": t, "source": source}}


def test_markets_line_shows_live_prices_moved_from_the_close_before_their_trading_day(dbfile, monkeypatch):
    _prices(dbfile, [(_day(2), "BTC", 90000.0, "c"), (_day(1), "BTC", 96000.0, "c"), (_day(0), "BTC", 99000.0, "c"),
                     (_day(4), "XAU", 4000.0, "y"), (_day(3), "XAU", 4100.0, "y"), (_day(1), "XAU", 4200.0, "y"),
                     (_day(5), "SPX", 6800.0, "y"), (_day(4), "SPX", 6900.0, "y"), (_day(3), "SPX", 7000.0, "y")])
    _meta(dbfile, "inflation", json.dumps({"pct": 3.1, "asof": _day(1)}))
    live = {**_live("BTC", 97920.0, _day(0)),                                  # today's stored candle is still moving
            **_live("XAU", 4230.0, _day(1), vs=4242.0, source="swissquote"),  # spot shown, the future measures the move
            **_live("SPX", 7140.0, _day(3), source="yahoo ^GSPC", t=1.0)}      # a weekend: the last session's move
    asked = []
    monkeypatch.setattr(server.live_markets, "quotes", lambda wait=5.0: asked.append(wait) or live)
    html = server.markets_line()
    assert asked == [server.live_markets.WAIT]
    assert _mkt(html) == (
        '<div class="mkt" title="Live prices; each move is from the close before its trading day">'
        '<div data-live><span class="n">Bitcoin</span><b title="Live price (coinbase), '
        f'{server._clock(live["BTC"]["time"])}">$97,920</b><i class="up" title="Since the close before, {_day(1)}">+2.0%</i></div>'
        '<div data-live><span class="n">Gold</span><b title="Live price (swissquote), '
        f'{server._clock(live["XAU"]["time"])}">$4,230</b><i class="up" title="Since the close before, {_day(3)}">+3.5%</i></div>'
        f'<div data-live><span class="n">S&amp;P 500</span><b title="Live price (yahoo ^GSPC), {server._when(1.0)}">7,140</b>'
        f'<i class="up" title="Since the close before, {_day(4)}">+3.5%</i></div>'
        '<div><span class="n">Inflation</span><b>3.1%</b>'  # not a price: as stored
        f'<i title="Consumer prices, year over year: the Cleveland Fed nowcast as of {_day(1)}">a year</i></div></div>')
    assert html.endswith(f'<p class="upd" title="Re-read every minute">Live {server._clock(live["BTC"]["at"])}</p>')
    assert "data-live" not in server.login_markets() and "$99,000" in server.login_markets()  # the sign-in page: stored


def test_markets_line_falls_back_to_the_stored_close_per_symbol(dbfile, monkeypatch):
    _prices(dbfile, [(_day(2), "BTC", 96000.0, "c"), (_day(1), "BTC", 97000.0, "c"),
                     (_day(2), "XAU", 4000.0, "y"), (_day(1), "XAU", 4100.0, "y"), (_day(1), "SPX", 6800.0, "y")])
    live = {**_live("BTC", 970_000.0, _day(0)),          # ten times the close: a broken quote, not a price
            **_live("XAU", 4120.0, _day(0), vs=None)}    # no price on the closes' instrument: no move
    monkeypatch.setattr(server.live_markets, "quotes", lambda wait=5.0: live)  # and the S&P 500 source is down
    html = _mkt(server.markets_line())
    assert f'<div><span class="n">Bitcoin</span><b>$97,000</b><i class="up" title="Since the close before, {_day(2)}">+1.0%</i></div>' in html
    assert '<div data-live><span class="n">Gold</span><b title="Live price (coinbase), ' in html and '$4,120</b></div>' in html
    assert '<div><span class="n">S&amp;P 500</span><b>6,800</b></div>' in html
    monkeypatch.setattr(server.live_markets, "quotes", lambda wait=5.0: {})  # all down or off: the sign-in page's line
    assert server.markets_line() == server.login_markets()
    logged = []
    monkeypatch.setattr(server, "log", logged.append)
    monkeypatch.setattr(server.live_markets, "quotes", lambda wait=5.0: 1 / 0)
    assert server.markets_line() == server.login_markets() and "ZeroDivisionError" in logged[0]  # never raises
    monkeypatch.setattr(server, "DB", str(dbfile.parent / "missing.db"))  # live, with nothing stored: no move
    monkeypatch.setattr(server.live_markets, "quotes", lambda wait=5.0: _live("BTC", 97000.0, _day(0)))
    assert _mkt(server.markets_line()).startswith('<div class="mkt" title="Live prices; each move is from the close '
                                                       'before its trading day"><div data-live><span class="n">Bitcoin</span>')


def test_sign_in_page_dashboard_and_api_show_the_same_markets_line(dbfile, monkeypatch):
    """The sign-in page and the dashboard show the same line, live prices included, from the shared cache: a page never
    waits on a source (it shows what is already fetched), /api/markets waits at most WAIT and needs no session. Only
    the allowlisted symbols ever show, whatever a source answers."""
    _prices(dbfile, [(_day(1), "BTC", 97000.0, "c")])
    asked = []
    live = {**_live("BTC", 98000.0, _day(0)), **_live("ETH", 3400.0, _day(0))}  # ETH: not on the list, never shown
    monkeypatch.setattr(server.live_markets, "quotes", lambda wait=5.0: asked.append(wait) or live)
    monkeypatch.setattr(server.live_markets, "enabled", lambda: True)
    sent = []
    h = server.Handler.__new__(server.Handler)
    h._send = lambda code, body, ctype, headers=(), csp=None, cache=None: sent.append((code, body.decode()))
    h._renewal = lambda: []
    h._deny = lambda p: sent.append((401, p))
    h._authed = lambda: False
    h._login_page()
    h.path = "/api/markets"
    h.do_GET()  # signed out
    h._authed = lambda: True
    h.path = "/"
    h.do_GET()
    assert asked == [0, server.live_markets.WAIT, 0]
    line = server.markets_line(wait=0)
    assert "$98,000" in line and "data-live" in line and "3,400" not in line and "ETH" not in line
    assert f'<div class="mktbar" id="mkts" data-poll="60">{line}</div>' in sent[0][1]
    assert sent[1][0] == 200 and json.loads(sent[1][1]) == {"html": line, "refresh_s": 60}
    assert f'<div class="mktbar" data-poll="60">{line}</div>' in sent[2][1]
    monkeypatch.setattr(server.live_markets, "enabled", lambda: False)  # off: neither page re-reads the line
    monkeypatch.setattr(server.live_markets, "quotes", lambda wait=5.0: {})
    h._login_page()
    h.do_GET()
    assert '<div class="mktbar" id="mkts" data-poll="">' in sent[3][1] and '<div class="mktbar" data-poll="">' in sent[4][1]
    assert "$97,000" in sent[3][1] and "<div data-live>" not in sent[3][1]


def test_markets_line_is_built_once_per_set_of_quotes(dbfile, monkeypatch):
    """Anonymous traffic costs one small query per new quote, not one per request."""
    _prices(dbfile, [(_day(1), "BTC", 97000.0, "c")])
    live = _live("BTC", 98000.0, _day(0))
    monkeypatch.setattr(server.live_markets, "quotes", lambda wait=5.0: live)
    built, build = [], server._market_closes_html
    monkeypatch.setattr(server, "_market_closes_html", lambda *a: built.append(1) or build(*a))
    first = server.markets_line()
    assert [server.markets_line() for _ in range(5)] == [first] * 5 and len(built) == 1
    live = _live("BTC", 98100.0, _day(0))  # the next fetch
    assert "$98,100" in server.markets_line() and len(built) == 2

def test_account_with_no_matching_label_is_filed_by_its_type():
    """A ledger without a spreadsheet has no row labels: networth.py passes each account's kind instead."""
    line = lambda name, kind, typ="asset": {"line": name, "type": typ, "kind": kind}
    assert server.categorize(line("Account 7", "savings")) == "cash"
    assert server.categorize(line("Account 7", "hsa")) == "retirement"
    assert server.categorize(line("Account 7", "investment")) == "taxable"
    assert server.categorize(line("Account 7", None)) == "other"
    assert server.categorize({"line": "Account 7", "type": "asset"}) == "other"  # sheet rows carry no kind
    assert server.categorize(line("Account 7", "credit", "liability")) == "cards"



# ---------- app shell: installable web app (PWA) and the Android app's asset links ----------
@pytest.fixture
def live(auth, monkeypatch):
    """The real handler on a free loopback port; requests carry no session."""
    import threading
    monkeypatch.setattr(server, "AUTH", auth)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def fetch(host: str, path: str, accept: str = "*/*"):
    import http.client
    c = http.client.HTTPConnection(host, timeout=10)
    c.request("GET", path, headers={"Accept": accept})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


def test_app_shell_files_are_public_with_their_types_and_no_session(live):
    want = {"/manifest.webmanifest": "application/manifest+json", "/sw.js": "text/javascript",
            "/offline.html": "text/html", "/pwa/icon-192.png": "image/png", "/pwa/icon-512.png": "image/png",
            "/pwa/icon-maskable-512.png": "image/png", "/pwa/apple-touch-icon.png": "image/png"}
    assert set(want) == set(server.PWA_FILES)
    for path, ctype in want.items():
        status, h, body = fetch(live, path)
        assert status == 200 and h["Content-Type"].startswith(ctype), (path, status, h)
        assert "Set-Cookie" not in h and body == (Path(server.PWA_DIR) / server.PWA_FILES[path][0]).read_bytes()
    # the worker's own fetches (its install cache, network-first pages) are allowed; nothing else is
    assert fetch(live, "/sw.js")[1]["Content-Security-Policy"].endswith("connect-src 'self'")


def test_manifest_names_the_app_and_every_icon_it_lists_is_served_at_its_size(live):
    import struct
    m = json.loads(fetch(live, "/manifest.webmanifest")[2])
    assert (m["name"], m["short_name"], m["start_url"], m["scope"], m["display"]) == (
        "Wealth Ledger", "Ledger", "/", "/", "standalone")
    assert m["theme_color"] == m["background_color"] == "#111110"  # the default (graphite) theme's --bg
    assert {i["purpose"] for i in m["icons"]} == {"any", "maskable"}
    assert {i["sizes"] for i in m["icons"] if i["purpose"] == "any"} == {"192x192", "512x512"}
    for i in m["icons"]:
        status, h, png = fetch(live, i["src"])
        assert status == 200 and png[:8] == b"\x89PNG\r\n\x1a\n"
        assert "%dx%d" % struct.unpack(">II", png[16:24]) == i["sizes"]


def test_both_pages_link_the_manifest_and_allow_it_and_the_worker_in_their_csp(live):
    page = server.LOGIN_HTML + (Path(server.HERE) / "index.html").read_text()
    assert page.count('<link rel="manifest" href="/manifest.webmanifest">') == 2
    assert page.count('navigator.serviceWorker.register("/sw.js")') == 2
    status, h, _ = fetch(live, "/login", "text/html")
    assert status == 200 and "manifest-src 'self'; worker-src 'self'" in h["Content-Security-Policy"]
    assert "manifest-src 'self'; worker-src 'self'" in server.index_csp(b"")


def test_assetlinks_is_public_and_built_from_personal_json(live):
    status, h, body = fetch(live, "/.well-known/assetlinks.json")
    assert status == 200 and h["Content-Type"] == "application/json" and "Set-Cookie" not in h
    ex = json.loads((Path(server.ROOT) / "personal.example.json").read_text())["dashboard"]["android"]
    assert json.loads(body) == [{"relation": ["delegate_permission/common.handle_all_urls"],
                                 "target": {"namespace": "android_app", "package_name": ex["package"],
                                            "sha256_cert_fingerprints": ex["sha256_cert_fingerprints"]}}]


@pytest.mark.parametrize("android", [None, "x", {}, {"package": "com.example.app"},
                                     {"package": "nodots", "sha256_cert_fingerprints": ["AA:" * 31 + "AA"]},
                                     {"package": "com.example.app", "sha256_cert_fingerprints": ["AA:BB", 5]},
                                     {"package": "com.example.app", "sha256_cert_fingerprints": "AA:" * 31 + "AA"}])
def test_assetlinks_is_empty_unless_configured_with_a_package_and_valid_fingerprints(monkeypatch, android):
    monkeypatch.setattr(server.personal, "section", lambda name: {"android": android} if android is not None else {})
    assert server.assetlinks() == []


def test_assetlinks_lists_every_valid_fingerprint_uppercased(monkeypatch):
    a, b = "ab:" * 31 + "ab", "CD:" * 31 + "CD"
    monkeypatch.setattr(server.personal, "section", lambda name: {"android": {
        "package": "com.example.app", "sha256_cert_fingerprints": [a, "junk", b]}})
    assert server.assetlinks()[0]["target"]["sha256_cert_fingerprints"] == [a.upper(), b]


def test_everything_else_still_needs_a_session(live):
    for path in ("/", "/index.html"):  # the page: to the sign-in page
        status, h, _ = fetch(live, path, "text/html")
        assert (status, h.get("Location")) == (303, "/login"), path
    for path in ("/api/summary", "/api/last", "/api/trend", "/pwa/", "/pwa/sw.js", "/sw.js/x", "/offline.html/x",
                 "/.well-known/other.json", "/healthz"):
        assert fetch(live, path)[0] == 401, path


def test_service_worker_caches_only_public_files_and_leaves_api_and_signed_in_pages_alone():
    sw = (Path(server.PWA_DIR) / "sw.js").read_text()
    # it never stores a response at run time: the only write to the cache is the install-time list of public files
    assert ".put(" not in sw and sw.count("addAll(") == 1
    node = shutil.which("node")
    if not node:
        pytest.skip("node isn't installed")
    script = """
      globalThis.self = {addEventListener() {}, location: {origin: "https://h.example"}};
      const {route, PUBLIC} = require(process.argv[1]);
      const r = (path, mode = "cors", method = "GET", origin = "https://h.example") =>
        route({url: origin + path, mode, method});
      console.log(JSON.stringify({PUBLIC, routes: {
        page: r("/", "navigate"), login: r("/login", "navigate"), post: r("/login", "navigate", "POST"),
        api: r("/api/summary"), apinav: r("/api/summary", "navigate"), last: r("/api/last"),
        offline: r("/offline.html"), icon: r("/pwa/icon-192.png"), other: r("/pwa/icon-512.png"),
        index: r("/index.html"), away: r("/", "navigate", "GET", "https://evil.example")}}));
    """
    out = json.loads(subprocess.run([node, "-e", script, str(Path(server.PWA_DIR) / "sw.js")], capture_output=True,
                                    text=True, check=True).stdout)
    assert set(out["PUBLIC"]) <= set(server.PWA_FILES)  # only files any visitor can fetch without a session
    assert out["routes"] == {"page": "page", "login": "page", "post": None, "api": None, "apinav": None, "last": None,
                             "offline": "shell", "icon": "shell", "other": None, "index": None, "away": None}
