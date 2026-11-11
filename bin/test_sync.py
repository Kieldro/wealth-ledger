"""wealth-ledger-sync's status file on the paths that never reach a bank: lock contention and "not claimed".
Runs the real script with HOME and every path pointed at a temp folder, so no feed, sheet or real file is touched."""
import fcntl
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "wealth-ledger-sync"


def run(tmp_path):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "WEALTH_LEDGER_ROOT": str(tmp_path / "no-repo"),
           "WEALTH_LEDGER_PYTHON": "/bin/false", "WEALTH_LEDGER_CONFIG_DIR": str(tmp_path / "conf"),
           "WEALTH_LEDGER_DB": str(tmp_path / "t.db"), "WEALTH_LEDGER_LOCK": str(tmp_path / "lock"),
           "WEALTH_LEDGER_STATUS": str(tmp_path / "status")}
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)


def test_run_blocked_by_a_stuck_sync_says_so_in_the_status_file(tmp_path):
    status = tmp_path / "status"
    status.write_text("last run 2026-01-01T08:15:00-06:00 rc=0\n")  # what the last good run left
    with open(tmp_path / "lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        r = run(tmp_path)
    assert r.returncode == 0 and "another sync is running" in r.stdout
    first, problem = status.read_text().splitlines()
    assert first.startswith("last run 20") and first.endswith(" rc=1") and "2026-01-01" not in first
    assert problem.startswith("Sync skipped: an earlier sync is still running or stuck")
    assert not (tmp_path / "status.tmp").exists()


def test_unclaimed_setup_exits_quietly_without_a_status_file(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0 and "not claimed yet" in r.stdout
    assert not (tmp_path / "status").exists()
