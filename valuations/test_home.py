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
