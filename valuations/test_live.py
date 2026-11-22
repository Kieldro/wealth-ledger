"""valuations/live.py: live repricing of the stored quantities, the price cache and its fallbacks. Offline: every
price source is replaced by a stand-in."""
import json
from datetime import date, datetime, timezone

import pytest

import ctdb
from valuations import live

TODAY = date.today().isoformat()
EARLIER = "2026-01-01T08:15:00+00:00"  # when the stored daily run fetched its prices


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.delenv(live.ENV, raising=False)  # the suite turns live prices off; these tests stand in for the sources
    live.reset()
    yield
    live.reset()


def rec(usd, source="test", t=None, at=None):
    return {"usd": usd, "source": source, "time": t, "at": at or datetime.now().timestamp()}


def quotes(crypto=None, metals=None, stocks=None):
    return {"crypto": {"prices": {k: rec(v) for k, v in (crypto or {}).items()}, "error": None},
            "metals": {"prices": {k: rec(v) for k, v in (metals or {}).items()}, "error": None},
            "stocks": {"prices": {k: (v if isinstance(v, dict) else rec(v)) for k, v in (stocks or {}).items()},
                       "error": None}}


CRYPTO = {"holdings": [
    {"wallet": "Cold", "coin": "BTC", "qty": 0.5, "price": 80000.0, "value": 40000.0, "counted": True},
    {"wallet": "Ex", "coin": "BTC", "qty": 0.25, "price": 80000.0, "value": 20000.0, "counted": False},
    {"wallet": "Ex", "coin": "ETH", "qty": 2.0, "price": 3000.0, "value": 6000.0, "counted": True},
    {"wallet": "Ex", "coin": "USD", "qty": 50.0, "price": 1.0, "value": 50.0, "counted": False}]}
METALS = {"metals": {"gold": {"oz": 2.0, "spot": 4000.0, "source": "swissquote", "value": 8000.0},
                     "silver": {"oz": 100.0, "spot": 50.0, "source": "swissquote", "value": 5000.0}}}


def val(detail, value, fetched=EARLIER):
    return {"date": TODAY, "value": value, "detail": detail, "fetched_at": datetime.fromisoformat(fetched).timestamp()}


def test_crypto_keeps_quantities_and_takes_live_prices():
    r = live.reprice_crypto(val(CRYPTO, 46000.0), quotes(crypto={"BTC": 90000.0}))
    rows = {(h["wallet"], h["coin"]): h for h in r["holdings"]}
    assert rows[("Cold", "BTC")]["value"] == 45000.0 and rows[("Cold", "BTC")]["qty"] == 0.5
    assert rows[("Ex", "BTC")]["value"] == 22500.0 and rows[("Ex", "BTC")]["counted"] is False  # not counted stays out
    assert rows[("Ex", "ETH")]["value"] == 6000.0 and rows[("Ex", "ETH")]["price"] == 3000.0  # no live ETH: stored price
    assert r["total"] == 51000.0
    assert r["by_coin"] == {"BTC": {"qty": 0.5, "price": 90000.0, "value": 45000.0},
                            "ETH": {"qty": 2.0, "price": 3000.0, "value": 6000.0}}
    assert r["when"]["sources"] == ["test"]


def test_crypto_falls_back_to_the_stored_run():
    assert live.reprice_crypto(val(CRYPTO, 46000.0), quotes()) is None  # no live price at all
    assert live.reprice_crypto(None, quotes(crypto={"BTC": 1.0})) is None  # no valuation yet
    newer = datetime.now(timezone.utc).replace(year=datetime.now().year + 1).isoformat()
    assert live.reprice_crypto(val(CRYPTO, 46000.0, fetched=newer), quotes(crypto={"BTC": 90000.0})) is None


def test_metals_are_ounces_times_live_spot():
    r = live.reprice_metals(val(METALS, 13000.0), quotes(metals={"Au": 4100.0}))
    assert r["metals"]["gold"]["value"] == 8200.0 and r["metals"]["gold"]["spot"] == 4100.0
    assert r["metals"]["silver"]["value"] == 5000.0  # no live silver: stored
    assert r["total"] == 13200.0
    assert live.reprice_metals(val(METALS, 13000.0), quotes()) is None


@pytest.mark.parametrize("sym, ok", [("VTI", True), ("BRK.B", True), ("FBTC", True), ("LIXMF", True),
                                     ("VBTLX", False), ("SPAXX", False), ("FDRXX", False), ("9889", False),
                                     ("", False), ("CUSIP123456", False)])
def test_quotable_leaves_out_cash_mutual_funds_and_made_up_ids(sym, ok):
    assert live.quotable(sym) is ok


def test_stocks_come_from_yahoo_spark_twenty_symbols_a_request(monkeypatch):
    asked = []

    def fake_get(url):
        syms = url.split("symbols=")[1].split("&")[0].replace("%2C", ",").split(",")
        asked.append(syms)
        return {s: {"timestamp": [1000, 1300, 1600], "close": [10.0, 11.0, None]} for s in syms if s != "GONE"}
    monkeypatch.setattr(live, "_get", fake_get)
    syms = [f"S{i:02d}" for i in range(23)] + ["BRK.B", "GONE"]
    got = live.fetch_stocks(syms)
    assert [len(a) for a in asked] == [20, 5]
    assert "BRK-B" in asked[0] + asked[1]  # Yahoo spells the share class with a dash
    assert got["BRK.B"] == (11.0, "yahoo", 1300.0)  # the last bar with a close
    assert "GONE" not in got and len(got) == 24


def test_prices_are_fetched_once_a_ttl_and_a_failure_keeps_the_last_good_ones(monkeypatch):
    calls = []

    def crypto_ok(keys):
        calls.append(keys)
        return {k: (100.0 + len(calls), "src", None) for k in keys}
    monkeypatch.setitem(live.FETCHERS, "crypto", crypto_ok)
    want = {"crypto": {"BTC"}}
    assert live.quotes(want)["crypto"]["prices"]["BTC"]["usd"] == 101.0
    assert live.quotes(want)["crypto"]["prices"]["BTC"]["usd"] == 101.0 and len(calls) == 1  # cached
    live.quotes({"crypto": {"BTC", "ETH"}})  # something new to price: fetched again at once
    assert len(calls) == 2
    monkeypatch.setattr(live, "TTL", 0)

    def crypto_down(keys):
        calls.append(keys)
        raise OSError("coingecko: timed out")
    monkeypatch.setitem(live.FETCHERS, "crypto", crypto_down)
    q = live.quotes(want)
    assert q["crypto"]["prices"]["BTC"]["usd"] == 102.0 and "timed out" in q["crypto"]["error"]
    monkeypatch.setattr(live, "MAX_AGE", -1)  # too old: dropped, so the stored daily valuation stands
    assert live.quotes(want)["crypto"]["prices"] == {}


def test_a_slow_source_does_not_hold_the_request(monkeypatch):
    import threading
    gate = threading.Event()

    def slow(keys):
        gate.wait(5)
        return {k: (1.0, "slow", None) for k in keys}
    monkeypatch.setitem(live.FETCHERS, "metals", slow)
    q = live.quotes({"metals": {"Au"}}, wait=0.05)
    assert q["metals"]["prices"] == {}  # not there yet: the stored price stands for this request
    gate.set()
    assert live.quotes({"metals": {"Au"}}, wait=2)["metals"]["prices"]["Au"]["usd"] == 1.0  # lands for the next


def test_off_and_stub_file(monkeypatch, tmp_path):
    monkeypatch.setitem(live.FETCHERS, "crypto", lambda keys: pytest.fail("network called"))
    monkeypatch.setenv(live.ENV, "off")
    assert live.quotes({"crypto": {"BTC"}})["crypto"]["prices"] == {}
    stub = tmp_path / "prices.json"
    monkeypatch.setenv(live.ENV, str(stub))
    assert not live.enabled()  # no file yet: off
    stub.write_text(json.dumps({"crypto": {"BTC": 70000}}))
    q = live.quotes({"crypto": {"BTC"}, "metals": {"Au"}})
    assert q["crypto"]["prices"]["BTC"]["usd"] == 70000.0 and q["metals"]["error"]  # metals "down"
    stub.write_text(json.dumps({"crypto": {"BTC": 71000}}))
    assert live.quotes({"crypto": {"BTC"}})["crypto"]["prices"]["BTC"]["usd"] == 71000.0  # read every time


# ---------------------------------------------------------------- networth lines

@pytest.fixture
def ledger(tmp_path):
    con = ctdb.connect(tmp_path / "t.db")
    con.execute("INSERT INTO account_kind VALUES ('ira','retirement',NULL), ('chk','checking',NULL)")
    con.execute(ctdb.UPSERT_BALANCE, (TODAY, "simplefin", "ira", "IRA (1)", "Broker", "1530.00", None, "USD", None))
    con.execute(ctdb.UPSERT_BALANCE, (TODAY, "simplefin", "chk", "Checking (2)", "Bank", "400.00", None, "USD", None))
    # 10 VTI @100, 500 money market, 2 of a mutual fund @10, 10 of cash outside any position
    for sym, sh, px in (("VTI", 10, 100.0), ("SPAXX", 500, 1.0), ("VBTLX", 2, 10.0)):
        con.execute(ctdb.UPSERT_HOLDING, (TODAY, "simplefin", "ira", "IRA (1)", "Broker", sym, sym, sym,
                                          str(sh), str(px), str(sh * px), "0", "USD"))
    for asset, value, detail in (("Total cryptocurrency (USD)", 46000.0, CRYPTO), ("Total metals value", 13000.0, METALS)):
        con.execute("INSERT INTO valuations VALUES (?,?,?,?,?,?)", (TODAY, asset, value, "x", json.dumps(detail), EARLIER))
    con.commit()
    yield con
    con.close()


def nw_result():
    lines = [{"line": "IRA", "type": "asset", "used": 1530.0, "from": "live", "synced": True, "accounts": ["ira"]},
             {"line": "Checking", "type": "asset", "used": 400.0, "from": "live", "synced": True, "accounts": ["chk"]},
             {"line": "Total cryptocurrency (USD)", "type": "asset", "used": 46000.0, "from": "estimate (x)",
              "synced": False},
             {"line": "Total metals value", "type": "asset", "used": 13000.0, "from": "estimate (x)", "synced": False},
             {"line": "Card", "type": "liability", "used": 30.0, "from": "live", "synced": True, "accounts": []}]
    return {"lines": lines, "assets": 60930.0, "liabilities": 30.0, "net_worth": 60900.0}


def test_wanted_lists_what_the_ledger_holds(ledger):
    assert live.wanted(ledger) == {"crypto": {"BTC", "ETH"}, "metals": {"Au", "Ag"}, "stocks": {"VTI"}}


def test_networth_lines_at_live_prices(ledger):
    q = quotes(crypto={"BTC": 90000.0, "ETH": 3500.0}, metals={"Au": 4100.0, "Ag": 55.0}, stocks={"VTI": 110.0})
    before = nw_result()
    r = live.reprice(before, ledger, q, today=TODAY)
    used = {l["line"]: l["used"] for l in r["lines"]}
    assert used == {"IRA": 1630.0, "Checking": 400.0,  # 10 VTI +$10 each; cash and the fund unchanged
                    "Total cryptocurrency (USD)": 52000.0, "Total metals value": 13700.0, "Card": 30.0}
    assert {l["line"]: l.get("stored") for l in r["lines"] if "stored" in l} == {
        "IRA": 1530.0, "Total cryptocurrency (USD)": 46000.0, "Total metals value": 13000.0}
    assert (r["assets"], r["liabilities"], r["net_worth"]) == (67730.0, 30.0, 67700.0)
    assert set(r["prices"]) == {"crypto", "metals", "stocks"}
    assert before["net_worth"] == 60900.0 and before["lines"][0]["used"] == 1530.0  # the input is left alone


def test_networth_falls_back_line_by_line(ledger):
    r = live.reprice(nw_result(), ledger, quotes(crypto={"BTC": 90000.0}), today=TODAY)  # metals and stocks down
    used = {l["line"]: l["used"] for l in r["lines"]}
    assert used["IRA"] == 1530.0 and used["Total metals value"] == 13000.0
    assert used["Total cryptocurrency (USD)"] == 51000.0 and set(r["prices"]) == {"crypto"}
    r = live.reprice(nw_result(), ledger, quotes(), today=TODAY)
    assert r["net_worth"] == 60900.0 and r["prices"] == {} and not any("stored" in l for l in r["lines"])
    sheet = nw_result()  # the sheet's own figure in use (no valuation counted): never replaced
    sheet["lines"][2].update(used=45000.0, **{"from": "sheet"})
    r = live.reprice(sheet, ledger, quotes(crypto={"BTC": 90000.0}), today=TODAY)
    assert {l["line"]: l["used"] for l in r["lines"]}["Total cryptocurrency (USD)"] == 45000.0


def test_an_implausible_quote_keeps_the_feed_price(ledger):
    """A quote far off the feed's own price is a different security or a split: the feed's value stands."""
    r = live.reprice(nw_result(), ledger, quotes(stocks={"VTI": 1000.0}), today=TODAY)
    assert {l["line"]: l["used"] for l in r["lines"]}["IRA"] == 1530.0
    assert live.holding_quote(quotes(stocks={"VTI": 1000.0}), "VTI", "retirement", 100.0) is None
    assert live.holding_quote(quotes(stocks={"VTI": 105.0}), "VTI", "retirement", 100.0)[1]["usd"] == 105.0
    # a coin at a synced exchange is priced as the coin (exchanges send no price), never as the fund ticker BTC
    q = quotes(crypto={"BTC": 90000.0}, stocks={"BTC": 40.0})
    assert live.holding_quote(q, "BTC", "crypto", None)[1]["usd"] == 90000.0
    assert live.holding_quote(q, "BTC", "retirement", 38.0)[1]["usd"] == 40.0
    assert live.holding_quote(q, "ETH2", "crypto", None) is None  # no live ETH


def test_a_quote_counts_as_the_close_of_its_own_trading_day():
    friday_4pm = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc).timestamp()
    closes = {"VTI": [("2026-11-19", 99.0), ("2026-11-20", 100.0)], "BND": [("2026-11-20", 70.0)]}
    out = live.stock_closes(closes, quotes(stocks={"VTI": rec(101.0, t=friday_4pm)}), "2026-11-22")
    assert out["VTI"] == [("2026-11-19", 99.0), ("2026-11-20", 101.0)] and out["BND"] == closes["BND"]
