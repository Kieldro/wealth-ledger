"""insights/live_markets.py: trading days, the per-symbol cache, the wait cap and the fallbacks, with every source
stubbed (no network)."""
import json
import threading
from datetime import datetime

import pytest

from insights import live_markets as lm


def ts(s: str, tz=lm.NY) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=tz).timestamp()


def test_trading_days():
    assert lm.btc_day(ts("2026-11-22T19:30")) == "2026-11-23"  # 7:30 PM New York is the next UTC day
    assert lm.spx_day(ts("2026-11-20T16:00")) == "2026-11-20"
    # COMEX: a session opens at 6 PM for the next day; the weekend is Friday's
    assert lm.gold_day(ts("2026-11-19T17:59")) == "2026-11-19"
    assert lm.gold_day(ts("2026-11-19T18:00")) == "2026-11-20"
    assert lm.gold_day(ts("2026-11-20T18:30")) == "2026-11-20"  # Friday evening: closed, still Friday
    assert lm.gold_day(ts("2026-11-21T12:00")) == "2026-11-20"  # Saturday
    assert lm.gold_day(ts("2026-11-22T12:00")) == "2026-11-20"  # Sunday before the open
    assert lm.gold_day(ts("2026-11-22T18:05")) == "2026-11-23"  # Sunday evening: Monday's session


@pytest.fixture
def live(monkeypatch):
    """Network mode with stubbed sources and a hand-moved clock: {sym: (usd, t, src, vs) or Exception or callable}."""
    monkeypatch.delenv(lm.ENV, raising=False)
    lm.reset()
    clock, answers, calls = [1000.0], {}, []

    def source(sym):
        def fetch():
            calls.append(sym)
            a = answers[sym]
            a = a() if callable(a) else a
            if isinstance(a, Exception):
                raise a
            return a
        return fetch
    monkeypatch.setattr(lm.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(lm, "SOURCES", {s: (source(s), d) for s, (_, d) in lm.SOURCES.items()})
    yield clock, answers, calls
    lm.reset()


def test_quotes_carry_price_day_source_and_the_comparable_price(live):
    clock, answers, calls = live
    t = ts("2026-11-20T15:00")
    answers.update(BTC=(97000.0, t, "coinbase", 97000.0), XAU=(4105.0, t, "swissquote", 4120.0),
                   SPX=(6811.6, t, "yahoo ^GSPC", 6811.6))
    q = lm.quotes()
    assert sorted(q) == ["BTC", "SPX", "XAU"] and sorted(calls) == ["BTC", "SPX", "XAU"]
    assert {k: q["XAU"][k] for k in ("usd", "vs", "day", "time", "source")} == \
        {"usd": 4105.0, "vs": 4120.0, "day": "2026-11-20", "time": t, "source": "swissquote"}
    assert q["BTC"]["day"] == "2026-11-20" and q["SPX"]["day"] == "2026-11-20"


def test_each_source_is_asked_at_most_once_a_ttl_a_failure_too(live, capsys):
    clock, answers, calls = live
    answers.update(BTC=(97000.0, 1.0, "coinbase", 97000.0), XAU=RuntimeError("swissquote: 503"), SPX=ValueError("no meta"))
    assert sorted(lm.quotes()) == ["BTC"]  # the down sources are left out: the strip shows their stored close
    assert lm.quotes() == lm.quotes() and len(calls) == 3  # within the TTL: from the cache, nothing asked again
    clock[0] += lm.TTL - 1
    lm.quotes()
    assert len(calls) == 3
    clock[0] += 2
    lm.quotes()
    assert sorted(calls[3:]) == ["BTC", "SPX", "XAU"]
    err = capsys.readouterr().err  # logged once when a source starts failing, not every minute; never notified
    assert err.count("live markets: XAU") == 1 and err.count("live markets: SPX") == 1


def test_a_slow_source_is_waited_for_at_most_the_cap_and_lands_for_the_next_call(live):
    clock, answers, calls = live
    gate = threading.Event()
    answers.update(BTC=lambda: gate.wait(5) and (97000.0, 1.0, "coinbase", 97000.0),
                   XAU=(4105.0, 1.0, "swissquote", None), SPX=(6811.6, 1.0, "yahoo ^GSPC", 6811.6))
    q = lm.quotes(wait=0.2)
    assert sorted(q) == ["SPX", "XAU"]  # bitcoin is still on its way
    assert sorted(lm.quotes(wait=0)) == ["SPX", "XAU"] and calls.count("BTC") == 1  # not asked twice while under way
    gate.set()
    for _ in range(100):
        if "BTC" in lm.quotes(wait=0.05):
            break
    assert lm.quotes(wait=0)["BTC"]["usd"] == 97000.0 and calls.count("BTC") == 1


def test_an_implausible_or_old_quote_is_dropped(live, monkeypatch):
    clock, answers, calls = live
    answers.update(BTC=(5.0, 1.0, "coinbase", 5.0), XAU=(4105.0, 1.0, "swissquote", 99.0), SPX=(6811.6, 1.0, "y", 6811.6))
    q = lm.quotes()
    assert "BTC" not in q and q["XAU"]["vs"] is None and q["SPX"]["usd"] == 6811.6  # a bad comparable: no move
    later = lm.time.time() + lm.MAX_AGE + 1
    answers.update(SPX=RuntimeError("down"))
    clock[0] += lm.TTL
    monkeypatch.setattr(lm.time, "time", lambda: later)
    assert "SPX" not in lm.quotes()  # the last good one is too old by now: the stored close stands


def test_off_and_file_modes(monkeypatch, tmp_path):
    lm.reset()
    monkeypatch.setattr(lm, "SOURCES", {s: (lambda: pytest.fail("network"), d) for s, (_, d) in lm.SOURCES.items()})
    monkeypatch.setenv(lm.ENV, "off")
    assert lm.quotes() == {} and not lm.enabled()
    path = tmp_path / "live.json"
    monkeypatch.setenv(lm.ENV, str(path))
    assert lm.quotes() == {} and not lm.enabled()  # no file: off
    path.write_text(json.dumps({"BTC": 97000, "SPX": "n/a"}))
    q = lm.quotes()
    assert lm.enabled() and sorted(q) == ["BTC"] and q["BTC"]["usd"] == q["BTC"]["vs"] == 97000.0
    path.write_text("{broken")
    assert lm.quotes() == {}


def test_fetch_gold_measures_the_move_on_the_future(monkeypatch):
    monkeypatch.setattr(lm.metals, "spot_price", lambda el: (4105.0, "swissquote"))
    monkeypatch.setattr(lm, "_yahoo_last", lambda sym: (4120.0, 1.0) if sym == "GC=F" else pytest.fail(sym))
    assert lm.fetch_gold()[::2] == (4105.0, "swissquote") and lm.fetch_gold()[3] == 4120.0
    monkeypatch.setattr(lm, "_yahoo_last", lambda sym: 1 / 0)
    assert lm.fetch_gold()[3] is None  # the spot still shows, without a move
    monkeypatch.setattr(lm.metals, "spot_price", lambda el: (4119.0, "yahoo futures"))
    assert lm.fetch_gold()[3] == 4119.0  # already the future


def test_fetch_btc_falls_back_to_coingecko(monkeypatch):
    def get(url):
        if "coinbase" in url:
            raise OSError("timed out")
        return {"bitcoin": {"usd": 96999.5}}
    monkeypatch.setattr(lm, "_get", get)
    usd, t, src, vs = lm.fetch_btc()
    assert (usd, src, vs) == (96999.5, "coingecko", 96999.5)
    monkeypatch.setattr(lm, "_get", lambda url: 1 / 0)
    with pytest.raises(RuntimeError, match="coinbase: .*coingecko: "):
        lm.fetch_btc()
