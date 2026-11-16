"""valuations/home.py Zillow source (Apify call mocked; no network, no real address)."""
import pytest

from valuations import home


class Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_zillow_reads_zestimate(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "t")
    monkeypatch.setattr(home.requests, "post", lambda *a, **k: Resp([{"zpid": 1, "zestimate": 300000,
                                                                       "rentZestimate": 2000, "livingArea": 1500}]))
    z = home.zillow()
    assert z["zestimate"] == 300000.0 and z["rent_zestimate"] == 2000 and z["url"].endswith("/1_zpid/")


def test_zillow_without_zestimate_is_an_error(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "t")
    monkeypatch.setattr(home.requests, "post", lambda *a, **k: Resp([{"zpid": 1}]))
    with pytest.raises(RuntimeError):
        home.zillow()


def test_home_is_due_once_a_month_from_the_first(tmp_path, monkeypatch):
    import sqlite3
    from datetime import date
    from valuations import home, run

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE valuations (snapshot_date TEXT, asset TEXT)")

    class Day(date):
        d = date(2026, 11, 16)

        @classmethod
        def today(cls):
            return cls.d
    monkeypatch.setattr(run, "date", Day)
    db.execute("INSERT INTO valuations VALUES ('2026-11-15', ?)", (home.ASSET,))
    assert not run.due(db, home)          # already done this month
    Day.d = date(2026, 12, 1)
    assert run.due(db, home)              # the 1st
    Day.d = date(2026, 12, 3)
    assert run.due(db, home)              # the 1st failed: keep trying
    db.execute("INSERT INTO valuations VALUES ('2026-12-03', ?)", (home.ASSET,))
    assert not run.due(db, home)
