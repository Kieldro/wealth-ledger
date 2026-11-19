#!/usr/bin/env python3
"""Run every valuation provider and store results in the `valuations` table (ctdb).

Each provider is a module in this package exposing
    NAME: str                       short id, e.g. "metals"
    EVERY_DAYS: int                 1 = daily, 30 = monthly (skipped if a newer snapshot exists)
    MONTHLY: bool                   optional: instead of EVERY_DAYS, once a month from the 1st (retried daily
                                    until one succeeds that month)
    ASSETS: tuple[str, ...]         balance-sheet labels it produces (used for the freshness check)
    fetch() -> list[dict]           [{asset: <balance-sheet label>, value: float USD, source: str, detail: dict}]
`asset` must be the exact balance-sheet row label in networth.py (e.g. "Total metals value", "Home");
networth.py then uses the latest valuation instead of the sheet's manual number.
Providers must never write the sheet, never send notifications, and only call free endpoints.

  run.py              all providers that are due
  run.py metals --force
"""
from __future__ import annotations

import importlib
import json
import os
import pkgutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctdb  # noqa: E402

DB = Path(os.environ.get("WEALTH_LEDGER_DB") or Path.home() / ".local/share/wealth-ledger/txns.db")


def providers():
    pkg = Path(__file__).parent
    for m in pkgutil.iter_modules([str(pkg)]):
        if m.name in ("run",) or m.name.startswith("_") or m.name.startswith("test"):
            continue
        try:
            mod = importlib.import_module(f"valuations.{m.name}")
        except ctdb.personal.PersonalConfigError:  # e.g. no "home" or "cars" section in personal.json
            print(f"{m.name}: not set up, skipped")
            continue
        if hasattr(mod, "fetch"):
            yield mod


def due(db, mod) -> bool:
    today = date.today()
    if getattr(mod, "MONTHLY", False):
        cutoff = today.replace(day=1).isoformat()
    else:
        cutoff = (today - timedelta(days=getattr(mod, "EVERY_DAYS", 1) - 1)).isoformat()
    for asset in getattr(mod, "ASSETS", ()):
        if not db.execute("SELECT 1 FROM valuations WHERE asset=? AND snapshot_date>=?", (asset, cutoff)).fetchone():
            return True
    return not getattr(mod, "ASSETS", ())


def stored_warnings(db, mod) -> list[str]:
    """The warnings a provider wrote into the detail of its newest stored result (a coin with no price, a wallet
    lookup that fell back to the sheet). Read from the table, not the run, so they are repeated on every sync of
    the day, including the ones where the provider is fresh and skipped."""
    out = []
    for asset in getattr(mod, "ASSETS", ()):
        row = db.execute("SELECT detail FROM valuations WHERE asset = ? ORDER BY snapshot_date DESC LIMIT 1",
                         (asset,)).fetchone()
        try:
            warns = (json.loads(row[0]) or {}).get("warnings") or []
        except (TypeError, ValueError, AttributeError):
            continue
        out += [w for w in warns if w not in out]
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    only = [a for a in argv if not a.startswith("-")]
    force = "--force" in argv
    db = ctdb.connect(DB)
    failed = 0
    mods = list(providers())
    for mod in mods:
        name = getattr(mod, "NAME", mod.__name__)
        if only and name not in only:
            continue
        if not force and not due(db, mod):
            print(f"{name}: fresh, skipped")
            continue
        try:
            rows = mod.fetch()
        except Exception as e:  # one provider failing must not stop the others
            failed += 1
            print(f"error: {name}: {e}", file=sys.stderr)
            continue
        now, today = datetime.now(timezone.utc).isoformat(), date.today().isoformat()
        with db:
            db.executemany("INSERT OR REPLACE INTO valuations VALUES (?,?,?,?,?,?)",
                           [(today, r["asset"], float(r["value"]), r["source"], json.dumps(r.get("detail") or {}), now)
                            for r in rows])
        print(f"{name}: " + ", ".join(f"{r['asset']}={r['value']:,.0f}" for r in rows))
    for mod in mods:  # after the results: the sync script lifts WARNING lines into the status file
        name = getattr(mod, "NAME", mod.__name__)
        if only and name not in only:
            continue
        for w in stored_warnings(db, mod):
            print(f"WARNING: {name}: {w}")
    db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
