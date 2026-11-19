"""valuations/run.py: warnings a provider stored are repeated on every run (synthetic DB, no network)."""
import json
import types

import ctdb
from valuations import run


def test_stored_warnings_are_printed_even_when_the_provider_is_fresh(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.db"
    db = ctdb.connect(path)
    today = run.date.today().isoformat()
    db.execute("INSERT INTO valuations VALUES (?,?,?,?,?,?)",
               ("2020-01-01", "Coins", 1.0, "s", json.dumps({"warnings": ["old, fixed since"]}), "x"))
    db.execute("INSERT INTO valuations VALUES (?,?,?,?,?,?)",
               (today, "Coins", 2.0, "s", json.dumps({"warnings": ["no price for FOO, not counted"]}), "x"))
    db.execute("INSERT INTO valuations VALUES (?,?,?,?,?,?)", (today, "Home", 3.0, "s", None, "x"))
    db.commit()
    db.close()
    mods = [types.SimpleNamespace(__name__="m", NAME="crypto", ASSETS=("Coins",), fetch=lambda: 1 / 0),
            types.SimpleNamespace(__name__="m", NAME="home", ASSETS=("Home",), fetch=lambda: 1 / 0)]
    monkeypatch.setattr(run, "DB", path)
    monkeypatch.setattr(run, "providers", lambda: iter(mods))  # read once per run
    assert run.main([]) == 0  # both fresh: fetch is never called
    out = capsys.readouterr().out.splitlines()
    assert out == ["crypto: fresh, skipped", "home: fresh, skipped",
                   "WARNING: crypto: no price for FOO, not counted"]
