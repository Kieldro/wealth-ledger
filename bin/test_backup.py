"""bin/wealth-ledger-backup against a throwaway database (no real data)."""
import gzip
import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("wealth-ledger-backup")


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "txns.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE txns (id TEXT, amount TEXT)")
    con.execute("INSERT INTO txns VALUES ('t1', '-12.34')")
    con.commit()
    con.close()
    dest = tmp_path / "backups"
    dest.mkdir()
    cfg = tmp_path / "personal.json"
    cfg.write_text(json.dumps({"backup": {"dir": str(dest), "keep_days": 30}}))
    return {"db": db, "dest": dest, "cfg": cfg,
            "env": {**os.environ, "WEALTH_LEDGER_DB": str(db), "WEALTH_LEDGER_PERSONAL": str(cfg),
                    "WEALTH_LEDGER_TODAY": "2026-11-01"}}


def run(e, **extra):
    return subprocess.run([str(SCRIPT)], env={**e["env"], **extra}, capture_output=True, text=True)


def test_writes_a_private_restorable_copy(env, tmp_path):
    r = run(env)
    assert r.returncode == 0, r.stderr
    out = env["dest"] / "txns-2026-11-01.db.gz"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(out.read_bytes()))
    assert sqlite3.connect(restored).execute("SELECT * FROM txns").fetchall() == [("t1", "-12.34")]
    assert [p.name for p in env["dest"].iterdir()] == [out.name]  # no temp files left behind


def test_prunes_old_dailies_but_keeps_month_starts(env):
    for d in ("2026-09-01", "2026-09-15", "2026-10-01", "2026-10-02", "2026-10-31"):
        (env["dest"] / f"txns-{d}.db.gz").write_bytes(b"x")
    assert run(env).returncode == 0
    assert sorted(p.name for p in env["dest"].iterdir()) == [
        "txns-2026-09-01.db.gz", "txns-2026-10-01.db.gz", "txns-2026-10-02.db.gz", "txns-2026-10-31.db.gz",
        "txns-2026-11-01.db.gz"]


def test_missing_folder_is_an_error_and_nothing_is_created(env):
    gone = env["dest"] / "unmounted"
    env["cfg"].write_text(json.dumps({"backup": {"dir": str(gone)}}))
    r = run(env)
    assert r.returncode == 1 and "missing (drive not mounted?)" in r.stderr
    assert not gone.exists()


def test_no_backup_section_means_off(env):
    env["cfg"].write_text("{}")
    r = run(env)
    assert r.returncode == 0 and "backups off" in r.stdout
    assert list(env["dest"].iterdir()) == []


def test_corrupt_database_fails_and_keeps_the_existing_copy(env):
    good = env["dest"] / "txns-2026-11-01.db.gz"
    good.write_bytes(b"earlier good copy")
    env["db"].write_bytes(b"not a database" * 100)
    r = run(env)
    assert r.returncode == 1 and r.stderr.splitlines()[-1].startswith("error: sqlite backup")  # the sync shows the last line
    assert good.read_bytes() == b"earlier good copy"
