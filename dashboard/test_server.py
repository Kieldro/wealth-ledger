"""Dashboard API helpers against a small synthetic database (no real data)."""
import json
from datetime import date

import pytest

import ctdb
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
    assert out["available"] == 1250.0 and out["util_pct"] == pytest.approx(16.7)


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
