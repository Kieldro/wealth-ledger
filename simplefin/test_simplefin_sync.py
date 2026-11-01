"""SimpleFIN response handling (synthetic responses, no network)."""
from datetime import datetime, timezone

import simplefin_sync as sf

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
DAY = 86400


def resp(*accts):
    return {"connections": [{"conn_id": "c1", "name": "Bank A"}, {"conn_id": "c2", "name": "Exchange B"}],
            "accounts": [{"id": f"a{i}", "conn_id": c, "balance-date": int(NOW.timestamp()) - age * DAY}
                         for i, (c, age) in enumerate(accts)]}


def test_connection_stuck_for_days_is_flagged_once():
    (w,) = sf.stale_warnings(resp(("c1", 0), ("c2", 16), ("c2", 20)), NOW)
    assert w.startswith("stale: Connection to Exchange B has not updated since 2026-10-16 (16 days)")
    assert "conn=c2" in w


def test_one_fresh_account_keeps_a_connection_fresh_and_weekends_are_ok():
    assert sf.stale_warnings(resp(("c1", 3), ("c2", 30), ("c2", 1)), NOW) == []


def test_missing_balance_date_is_not_stale():
    data = resp(("c1", 0))
    data["accounts"].append({"id": "x", "conn_id": "c2"})
    assert sf.stale_warnings(data, NOW) == []
