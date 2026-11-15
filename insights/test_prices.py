"""insights/prices.py against synthetic replies (no network)."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import ctdb
from insights import prices


def _ts(d: str) -> int:
    return int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp())


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):  # main() also fetches the inflation nowcast: never from a test
    def refuse(url, timeout=60):
        raise RuntimeError(f"test tried to reach {url}")
    monkeypatch.setattr(prices, "_get_gz", refuse)


def _nowcast(*months):
    """The Cleveland Fed file's shape: one chart per (month, as-of date, CPI values), oldest first."""
    return [{"chart": {"_comment": f"{asof} 00:00", "subcaption": month},
             "dataset": [{"seriesname": "Core CPI Inflation", "data": [{"value": "9.9"}]},
                         {"seriesname": "CPI Inflation", "data": [{"value": v} for v in vals]}]}
            for month, asof, vals in months]


def test_cleveland_nowcast_takes_the_latest_cpi_estimate_of_the_newest_month_that_has_one():
    file = _nowcast(("2026-10", "2026-10-30", ["3.80", "3.85"]), ("2026-11", "2026-11-13", ["3.90", "3.93543014449254", ""]),
                    ("2026-12", "2026-11-13", ["", ""]))
    assert prices.cleveland_nowcast(lambda url: file) == {"pct": 3.94, "asof": "2026-11-13", "month": "2026-11",
                                                         "source": "Cleveland Fed nowcast"}
    for bad in ([], _nowcast(("2026-11", "2026-11-13", ["250"])), _nowcast(("2026-11", "soon", ["3.9"]))):
        with pytest.raises((RuntimeError, ValueError)):
            prices.cleveland_nowcast(lambda url, bad=bad: bad)


def test_update_inflation_stores_json_and_main_survives_its_failure(con, tmp_path, monkeypatch, capsys):
    good = {"pct": 3.94, "asof": "2026-11-13", "month": "2026-11", "source": "Cleveland Fed nowcast"}
    stored = lambda: con.execute("SELECT value FROM meta WHERE key = 'inflation'").fetchone()
    assert prices.update_inflation(con, dry_run=True, fetch=lambda: good) == good and stored() is None
    prices.update_inflation(con, fetch=lambda: good)
    assert json.loads(stored()[0]) == good
    with pytest.raises(RuntimeError, match="inflation: KeyError"):
        prices.update_inflation(con, fetch=lambda: {}["x"])
    assert json.loads(stored()[0]) == good  # a failed fetch keeps the last good figure
    monkeypatch.setattr(prices, "update", lambda db, dry_run=False: {})
    monkeypatch.setattr(prices, "cleveland_nowcast", lambda: 1 / 0)
    assert prices.main(["--db", str(tmp_path / "i.db")]) == 0  # the prices still count as updated
    assert "inflation not updated (inflation: ZeroDivisionError" in capsys.readouterr().out


def fake_coinbase(close_of):
    """A Coinbase candles endpoint over synthetic closes: honours start/end, caps at 300, newest first."""
    calls = []

    def get(url):
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(url).query)
        s = date.fromisoformat(qs["start"][0][:10])
        e = date.fromisoformat(qs["end"][0][:10])
        calls.append((s, e))
        days = [s + timedelta(days=i) for i in range((e - s).days + 1)]
        assert len(days) <= 300, "asked for more than one page"
        return [[_ts(d.isoformat()), 1, 2, 1, close_of(d), 5] for d in reversed(days)]
    return get, calls


def test_coinbase_pages_300_days_and_keeps_utc_dates(monkeypatch):
    get, calls = fake_coinbase(lambda d: 1000.0 + d.toordinal() % 1000)
    monkeypatch.setattr(prices, "_get", get)
    slept = []
    out = prices.coinbase_candles("BTC-USD", date(2024, 1, 1), date(2025, 6, 30), sleep=slept.append)
    n = (date(2025, 6, 30) - date(2024, 1, 1)).days + 1
    assert len(out) == n and len(calls) == 2 and len(slept) == 1
    assert calls[0] == (date(2024, 1, 1), date(2024, 10, 26)) and calls[1][0] == date(2024, 10, 27)
    assert out["2024-01-01"] == 1000.0 + date(2024, 1, 1).toordinal() % 1000


def test_yahoo_dates_in_exchange_time_and_skips_nulls(monkeypatch):
    # Yahoo stamps COMEX days at midnight New York (04:00/05:00 UTC); a null close is a missing day
    reply = {"chart": {"result": [{"meta": {"gmtoffset": -18000, "exchangeTimezoneName": "America/New_York"},
                                   "timestamp": [_ts("2026-10-29T04:00:00"), _ts("2026-10-30T04:00:00"),
                                                 _ts("2026-11-02T05:00:00")],
                                   "indicators": {"quote": [{"close": [4031.4, None, 3900.0]}]}}], "error": None}}
    monkeypatch.setattr(prices, "_get", lambda url: reply)
    assert prices.yahoo_daily("GC=F", date(2026, 10, 1), date(2026, 11, 2)) == {
        "2026-10-29": 4031.4, "2026-11-02": 3900.0}
    monkeypatch.setattr(prices, "_get", lambda url: {"chart": {"result": None, "error": {"code": "Not Found"}}})
    with pytest.raises(RuntimeError, match="Not Found"):
        prices.yahoo_daily("GC=F", date(2026, 10, 1), date(2026, 11, 2))


def test_fetch_falls_back_and_drops_implausible(monkeypatch):
    def broken(s, e):
        raise OSError("timed out")
    monkeypatch.setitem(prices.SOURCES, "XAU", [
        ("first", broken), ("second", lambda s, e: {"2026-10-30": 3900.0, "2026-10-29": 1.0})])
    assert prices.fetch("XAU", date(2026, 10, 29), date(2026, 10, 30)) == ({"2026-10-30": 3900.0}, "second")
    monkeypatch.setitem(prices.SOURCES, "XAU", [("first", broken)])
    with pytest.raises(RuntimeError, match="XAU: first: timed out"):
        prices.fetch("XAU", date(2026, 10, 29), date(2026, 10, 30))


def test_weekdays_only_for_gold_token():
    assert prices._weekdays({"2026-10-30": 1.0, "2026-10-31": 2.0, "2026-11-01": 3.0, "2026-11-02": 4.0}) == {
        "2026-10-30": 1.0, "2026-11-02": 4.0}


@pytest.fixture
def con(tmp_path):
    db = ctdb.connect(tmp_path / "p.db")
    db.execute("INSERT INTO networth_history (date, net_worth) VALUES ('2025-03-10', 1.0)")
    db.execute(ctdb.UPSERT_BALANCE, ("2026-10-16", "plaid", "a1", "A", "Bank", "1", None, "USD", None))
    db.commit()
    yield db
    db.close()


def test_backfill_then_incremental(con, monkeypatch):
    asked = []

    def src(sym):
        def fn(s, e):
            asked.append((sym, s, e))
            days = [s + timedelta(days=i) for i in range((e - s).days + 1)]
            if sym == "XAU":
                days = [d for d in days if d.weekday() < 5]
            return {d.isoformat(): (50000.0 if sym == "BTC" else 2500.0) + i for i, d in enumerate(days)}
        return fn
    monkeypatch.setattr(prices, "SOURCES", {"BTC": [("cb", src("BTC"))], "XAU": [("yh", src("XAU"))]})
    today = date(2026, 11, 1)
    rep = prices.update(con, today=today)
    start = date(2025, 3, 10) - timedelta(days=prices.LEAD_DAYS)
    assert asked == [("BTC", start, today), ("XAU", start, today)]
    assert rep["BTC"]["rows"] == (today - start).days + 1 and rep["XAU"]["source"] == "yh"
    lo, n = con.execute("SELECT MIN(date), COUNT(*) FROM prices WHERE symbol='XAU'").fetchone()
    assert lo == "2025-03-03" and con.execute(
        "SELECT COUNT(*) FROM prices WHERE symbol='XAU' AND strftime('%w', date) IN ('0','6')").fetchone()[0] == 0

    asked.clear()  # next day: only the recent tail, one range per symbol
    prices.update(con, today=today + timedelta(days=1))
    tail = (today - timedelta(days=prices.REFETCH_DAYS), today + timedelta(days=1))
    assert asked == [("BTC", *tail), ("XAU", today - timedelta(days=2 + prices.REFETCH_DAYS), tail[1])]

    asked.clear()  # an older history point appears: backfill just the gap before the stored range
    con.execute("INSERT INTO networth_history (date, net_worth) VALUES ('2024-12-31', 1.0)")
    prices.update(con, today=today + timedelta(days=1), dry_run=True)
    assert asked[0] == ("BTC", date(2024, 12, 24), start)


def test_update_records_when_it_last_fetched_but_not_on_a_dry_run(con, monkeypatch):
    monkeypatch.setattr(prices, "SOURCES", {"BTC": [("cb", lambda s, e: {e.isoformat(): 50000.0})]})
    when = lambda: con.execute("SELECT value FROM meta WHERE key = 'prices_updated'").fetchone()
    prices.update(con, today=date(2026, 11, 1), dry_run=True)
    assert when() is None
    prices.update(con, today=date(2026, 11, 1))
    assert datetime.fromisoformat(when()[0]).utcoffset() == timedelta(0)  # stored in UTC, with its offset
    monkeypatch.setattr(prices, "SOURCES", {"BTC": [("cb", lambda s, e: {})]})
    before = when()
    with pytest.raises(RuntimeError):
        prices.update(con, today=date(2026, 11, 2))
    assert when() == before  # nothing came back: the time of the last real refresh stands


def test_update_reports_failures_but_keeps_the_rest(con, monkeypatch):
    def broken(s, e):
        raise OSError("down")
    monkeypatch.setattr(prices, "SOURCES", {"BTC": [("cb", broken)],
                                            "XAU": [("yh", lambda s, e: {"2026-10-30": 3900.0})]})
    with pytest.raises(RuntimeError, match="BTC: cb: down"):
        prices.update(con, today=date(2026, 11, 1))
    assert con.execute("SELECT symbol, usd FROM prices").fetchall() == [("XAU", 3900.0)]


def test_main_prints_one_error_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(prices, "update", lambda db, dry_run=False: (_ for _ in ()).throw(RuntimeError("BTC: x")))
    assert prices.main(["--db", str(tmp_path / "e.db")]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert err == ["error: prices: BTC: x"]


def test_asof_nearest_earlier_date():
    s = [("2026-10-29", 1.0), ("2026-10-30", 2.0), ("2026-11-02", 3.0)]
    assert prices.asof(s, ["2026-10-28", "2026-10-29", "2026-11-01", "2026-11-05"]) == {
        "2026-10-29": 1.0, "2026-11-01": 2.0, "2026-11-05": 3.0}
    assert prices.asof([], ["2026-10-29"]) == {}


def test_currencies_are_stored_inverted_with_their_tiny_digits(con, monkeypatch):
    """IRR=X / KWD=X quote units per dollar; the table holds USD per unit, kept to 8 significant digits."""
    rate = {"IRR=X": 1860475.0, "KWD=X": 0.3082}
    monkeypatch.setattr(prices, "yahoo_daily", lambda sym, s, e: {"2026-10-30": rate[sym]})
    monkeypatch.setattr(prices, "SOURCES", {k: prices.SOURCES[k] for k in ("IRR", "KWD")})
    prices.update(con, today=date(2026, 10, 30))
    got = dict(con.execute("SELECT symbol, usd FROM prices").fetchall())
    assert got["IRR"] == pytest.approx(1 / 1860475, rel=1e-7) and got["IRR"] > 0
    assert got["KWD"] == pytest.approx(3.2446464, rel=1e-7)


def test_every_symbol_has_a_source_and_a_plausible_range():
    assert set(prices.SYMBOLS + prices.BENCHMARKS) == set(prices.SOURCES) == set(prices.PLAUSIBLE)
    assert not set(prices.BENCHMARKS) & set(prices.SYMBOLS)  # overlays only, never a display unit


def test_indexes_backfill_the_whole_trend_from_yahoo(con, monkeypatch):
    """S&P 500 / Nasdaq: full history back to the first networth_history point, from ^GSPC / ^IXIC."""
    urls = []

    def get(url):
        urls.append(url)
        return {"chart": {"result": [{"meta": {"exchangeTimezoneName": "America/New_York"},
                                      "timestamp": [_ts("2025-03-07T05:00:00"), _ts("2026-10-30T04:00:00")],
                                      "indicators": {"quote": [{"close": [5770.2, 6840.0]}]}}], "error": None}}
    monkeypatch.setattr(prices, "_get", get)
    monkeypatch.setattr(prices, "SOURCES", {k: prices.SOURCES[k] for k in prices.BENCHMARKS})
    today = date(2026, 11, 1)
    assert prices.plan(con, "SPX", today) == [(date(2025, 3, 3), today)]
    rep = prices.update(con, today=today)
    assert rep["SPX"]["source"] == "yahoo ^GSPC" and rep["IXIC"]["source"] == "yahoo ^IXIC"
    assert "/chart/%5EGSPC?" in urls[0] and "/chart/%5EIXIC?" in urls[1]
    assert con.execute("SELECT date, usd FROM prices WHERE symbol='SPX' ORDER BY date").fetchall() == [
        ("2025-03-07", 5770.2), ("2026-10-30", 6840.0)]


def test_currencies_fetch_only_recent_days(con):
    con.execute("INSERT INTO networth_history (date, net_worth) VALUES ('2020-04-21', 1.0)")
    today = date(2026, 11, 1)
    assert prices.plan(con, "IRR", today) == [(today - timedelta(days=prices.LEAD_DAYS), today)]
    assert prices.plan(con, "XAG", today)[0][0] == date(2020, 4, 14)
