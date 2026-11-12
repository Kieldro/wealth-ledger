"""networth.compute against a synthetic database and a made-up balance sheet (personal.example.json rows)."""
import pytest

import ctdb
import networth

DAY = "2026-10-27"
SHEET = {"Checking": 900.0, "Savings": 4000.0, "Brokerage": 10000.0, "Roth IRA": 7000.0, "Cash": 200.0,
         "Total metals value": 1500.0, "Home": 300000.0, "Mortgage": 210000.0, "Credit card": 100.0,
         "Total Equity": 123.0, "Gold spot": 4000.0}
ACCOUNTS = [  # account_id, name, kind, balance
    ("chk", "Example Bank Checking (0000)", "checking", "1000.00"),
    ("sav", "Example Bank Savings (1111)", "savings", "5000.00"),
    ("brk", "Individual Brokerage (2222)", "investment", "12000.00"),
    ("ira", "ROTH IRA (3333)", "retirement", "8000.00"),
    ("mtg", "MORTGAGE LOAN 4444", "loan", "-200000.00"),
    ("cc1", "Example Rewards Card (5555)", "credit", "-300.00"),
    ("cc2", "Other Card (6666)", "credit", "-50.00"),          # no sheet row: added as its own line
    ("kid", "Custodial (7777)", "custodial", "999.00"),        # never the owner's money
    ("new", "Unclassified (8888)", None, "77.00"),             # no kind yet: not counted
]


@pytest.fixture
def nw(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    con = ctdb.connect(path)
    for aid, name, kind, bal in ACCOUNTS:
        if kind:
            con.execute("INSERT INTO account_kind VALUES (?,?,NULL)", (aid, kind))
        con.execute(ctdb.UPSERT_BALANCE, (DAY, "test", aid, name, "Bank", bal, None, "USD", None))
    con.execute("INSERT INTO valuations VALUES (?,?,?,?,?,?)", (DAY, "Home", 320000.0, "avm", None, DAY))
    con.commit()
    monkeypatch.setattr(networth, "DB", str(path))
    monkeypatch.setattr(networth, "sheet_rows", lambda: dict(SHEET))
    return con


def used(result):
    return {l["line"]: (l["type"], l["used"], l["from"].split(" (")[0]) for l in result["lines"]}


def test_valuation_is_live_only_when_read_today_with_nothing_typed_in():
    f, today = networth.valuation_from, "2026-10-27"
    assert f("exchange-api USD balance", today, today) == "live reading (exchange-api USD balance)"
    assert f("exchange-api USD balance", "2026-10-26", today) == "estimate (exchange-api USD balance, 2026-10-26)"
    assert f("qty: onchain/exchange-api; prices: coingecko, par", today, today).startswith("live reading (")
    assert f("qty: onchain/exchange-api/sheet; prices: coingecko", today, today).startswith("estimate (")
    assert f("qty: onchain; prices: coingecko", "2026-10-20", today).startswith("estimate (")
    for modelled in ("spot: swissquote", "comehome_avm+zillow_zestimate", "autotempest-comps"):
        assert f(modelled, today, today) == f"estimate ({modelled}, {today})"


def test_exchange_cash_read_today_is_a_live_line_but_not_a_synced_account(nw, monkeypatch):
    monkeypatch.setattr(networth, "ASSET_ROWS", networth.ASSET_ROWS | {"Exchange A", "Exchange B"})
    monkeypatch.setattr(networth, "sheet_rows", lambda: dict(SHEET, **{"Exchange A": 10.0, "Exchange B": 20.0}))
    nw.execute("INSERT INTO valuations VALUES (?,?,?,?,?,?)", (DAY, "Exchange A", 55.0, "exchange-api USD balance", None, DAY))
    nw.execute("INSERT INTO valuations VALUES (?,?,?,?,?,?)",
               ("2026-10-20", "Exchange B", 66.0, "exchange-api USD balance", None, "2026-10-20"))
    nw.commit()
    lines = {l["line"]: l for l in networth.compute(today=DAY)["lines"]}
    assert (lines["Exchange A"]["used"], lines["Exchange A"]["from"]) == (55.0, "live reading (exchange-api USD balance)")
    assert (lines["Exchange B"]["used"], lines["Exchange B"]["from"]) == (66.0, "estimate (exchange-api USD balance, 2026-10-20)")
    assert lines["Home"]["from"].startswith("estimate (avm")
    assert {l["line"] for l in lines.values() if l["synced"]} == {
        "Checking", "Savings", "Brokerage", "Roth IRA", "Mortgage", "Credit card", "Other Card (6666)"}


def test_live_balances_replace_sheet_rows_and_the_rest_keeps_its_source(nw):
    r = networth.compute()
    assert used(r) == {
        "Checking": ("asset", 1000.0, "live"), "Savings": ("asset", 5000.0, "live"),
        "Brokerage": ("asset", 12000.0, "live"), "Roth IRA": ("asset", 8000.0, "live"),
        "Cash": ("asset", 200.0, "sheet"), "Total metals value": ("asset", 1500.0, "sheet"),
        "Home": ("asset", 320000.0, "estimate"),
        "Mortgage": ("liability", 200000.0, "live"), "Credit card": ("liability", 300.0, "live"),
        "Other Card (6666)": ("liability", 50.0, "live"),
    }
    assert (r["assets"], r["liabilities"], r["net_worth"]) == (347700.0, 200350.0, 147350.0)
    assert r["sheet_net_worth"] == 123.0 and r["warnings"] == []


def test_renamed_account_is_reported_instead_of_silently_counted_twice(nw):
    nw.execute("UPDATE balances SET account_name='Example Checking (0000)' WHERE account_id='chk'")
    nw.commit()
    r = networth.compute()
    lines = used(r)
    assert lines["Checking"] == ("asset", 900.0, "sheet") and lines["Example Checking (0000)"][1] == 1000.0
    (w,) = r["warnings"]
    assert w.startswith('Net worth: no synced account matches the "Checking" row any more')


def test_balance_that_stopped_updating_is_counted_but_named(nw):
    nw.execute("UPDATE balances SET snapshot_date='2026-09-30' WHERE account_id IN ('sav', 'kid')")
    nw.execute("UPDATE balances SET snapshot_date='2026-10-21' WHERE account_id='ira'")  # 6 days behind: fine
    nw.commit()
    r = networth.compute()
    assert used(r)["Savings"] == ("asset", 5000.0, "live")
    (w,) = r["warnings"]  # the custodial account isn't counted, so it isn't named
    assert w.startswith("Net worth: Example Bank Savings (1111) was last updated 2026-09-30 and is still counted")


def test_each_line_says_what_day_its_number_is_from(nw):
    nw.execute("UPDATE balances SET as_of='2026-10-25T07:00:00' WHERE account_id='chk'")      # the bank's own date wins
    nw.execute("UPDATE balances SET snapshot_date='2026-10-24' WHERE account_id IN ('sav', 'cc2')")
    nw.commit()
    as_of = {l["line"]: l["as_of"] for l in networth.compute(today=DAY)["lines"]}
    assert as_of == {"Checking": "2026-10-25", "Savings": "2026-10-24", "Brokerage": DAY, "Roth IRA": DAY,
                     "Mortgage": DAY, "Credit card": DAY, "Other Card (6666)": "2026-10-24",
                     "Home": DAY, "Cash": None, "Total metals value": None}


def test_account_without_a_balance_does_not_break_the_total(nw):
    nw.execute("UPDATE balances SET balance=NULL WHERE account_id IN ('chk', 'cc2')")
    nw.commit()
    r = networth.compute()
    assert used(r)["Checking"] == ("asset", 0.0, "live") and "Other Card (6666)" not in used(r)
    assert r["warnings"] == ["Net worth: Example Bank Checking (0000) has no balance from its feed and is counted as 0."]
