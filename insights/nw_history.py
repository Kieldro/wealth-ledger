#!/usr/bin/env python3
"""Past whole-net-worth points from the yearly "Financial Statements YYYY" Google Sheets, so the dashboard trend
reaches back before the daily balance snapshots began. Reads Drive and Sheets with gws (read-only; never writes a
spreadsheet) and replaces the rows of the networth_history table.

Each yearly file has a balance-sheet tab: labels in column A, row 1 holds a snapshot date per column (a date
serial or an "M/D/YYYY" string; "Current" or blank headers are undated and skipped), and the "Total Equity" row
(plus "Total Assets" / "Total Liabilities") holds that snapshot's totals. A column whose header is a TODAY()/NOW()
formula, or is dated today or later, is the live column, not history: the live value comes from the daily series.
Error cells (#REF!) and blanks are skipped. The same date in several files keeps the file whose year matches it.

  python -m insights.nw_history               fetch and store
  python -m insights.nw_history --dry-run     show the dates found, write nothing
  python -m insights.nw_history --if-stale    only fetch when the last fetch is older than 7 days (daily sync)
Never sends notifications (owner's rule). Which files: personal.json "networth_history": {"name_query": "..."}
(default "Financial Statements"); a file counts when its name is that text plus a 4-digit year.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctdb  # noqa: E402
import personal  # noqa: E402

DEFAULT_DB = Path(os.environ.get("WEALTH_LEDGER_DB") or "~/.local/share/wealth-ledger/txns.db").expanduser()
DEFAULT_QUERY = "Financial Statements"
STALE_DAYS = 7
RANGE = "A1:AZ400"
ROWS = {"net_worth": "total equity", "assets": "total assets", "liabilities": "total liabilities"}
SHEETS_EPOCH = date(1899, 12, 30)  # day 0 of Google Sheets date serials
LIVE_FORMULA = re.compile(r"\b(TODAY|NOW)\s*\(", re.I)


def col_letter(i: int) -> str:
    """0-based column index -> A, B, ..., Z, AA, ..."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def header_date(v) -> date | None:
    """A row-1 header -> its date: a Sheets serial number or an M/D/YYYY (or YYYY-MM-DD) string; else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return SHEETS_EPOCH + timedelta(days=int(v)) if 20000 < v < 80000 else None  # ~1954..2119
    s = str(v).strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _num(row: list, i: int) -> float | None:
    v = row[i] if i < len(row) else None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_grid(values: list[list], formulas: list | None = None, today: date | None = None) -> list[dict]:
    """A balance-sheet grid (UNFORMATTED_VALUE rows) -> [{date, col, net_worth, assets, liabilities}] for every
    dated, historical column. formulas is row 1 as FORMULA render (to spot a TODAY() header)."""
    if not values:
        return []
    today = today or date.today()
    header, formulas = values[0], formulas or []
    rows = {}
    for r in values[1:]:
        if r and isinstance(r[0], str):
            rows.setdefault(r[0].strip().lower(), r)  # first occurrence wins
    out = []
    for i in range(1, len(header)):
        f = formulas[i] if i < len(formulas) else None
        if isinstance(f, str) and LIVE_FORMULA.search(f):
            continue
        d = header_date(header[i])
        if d is None or d >= today:
            continue
        pt = {k: _num(rows[lbl], i) if lbl in rows else None for k, lbl in ROWS.items()}
        if pt["net_worth"] is None and pt["assets"] is not None and pt["liabilities"] is not None:
            pt["net_worth"] = pt["assets"] - pt["liabilities"]
        if pt["net_worth"] is None:
            continue
        out.append({"date": d.isoformat(), "col": col_letter(i), **pt})
    return out


def merge(per_file: list[tuple[str, int, list[dict]]]) -> list[dict]:
    """[(file name, file year, points)] -> one point per date, ascending. A date in several files keeps the file
    whose year is the date's year, else the nearest year, else the newer file."""
    best: dict[str, tuple] = {}
    for name, year, pts in per_file:
        for p in pts:
            rank = (abs(int(p["date"][:4]) - year), -year)
            if p["date"] not in best or rank < best[p["date"]][0]:
                best[p["date"]] = (rank, {**p, "source": f"{name}, column {p['col']}"})
    return [best[d][1] for d in sorted(best)]


def _gws(*args: str) -> dict:
    out = subprocess.run(["gws", *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"gws failed: {(out.stderr or out.stdout).strip()[:200]}")
    return json.loads(out.stdout)


def clean_name(name: str) -> str:
    """Drive names can carry invisible sort-prefix characters (e.g. U+007F); keep printable text only."""
    return re.sub(r"\s+", " ", "".join(c for c in name if c.isprintable())).strip()


def find_files(query: str) -> list[tuple[str, str, int]]:
    """[(spreadsheet id, clean name, year)] of the spreadsheets named '<query> YYYY'."""
    q = (f"name contains {json.dumps(query)} and mimeType='application/vnd.google-apps.spreadsheet' "
         "and trashed=false")
    want = re.compile(rf"{re.escape(query)}\s+(\d{{4}})", re.I)
    files, page = [], None
    while True:
        params = {"q": q, "fields": "nextPageToken,files(id,name)", "pageSize": 100, **({"pageToken": page} if page else {})}
        res = _gws("drive", "files", "list", "--params", json.dumps(params))
        for f in res.get("files") or []:
            name = clean_name(f.get("name") or "")
            if m := want.fullmatch(name):
                files.append((f["id"], name, int(m.group(1))))
        page = res.get("nextPageToken")
        if not page:
            return sorted(files, key=lambda f: f[2])


def balance_tab(sid: str) -> str | None:
    titles = [s["properties"]["title"] for s in _gws(
        "sheets", "spreadsheets", "get", "--params",
        json.dumps({"spreadsheetId": sid, "fields": "sheets.properties.title"})).get("sheets") or []]
    exact = [t for t in titles if t.strip().lower() == "balance sheet"]
    loose = [t for t in titles if "balance" in t.lower()]
    return (exact or loose or [None])[0]


def _values(sid: str, rng: str, render: str) -> list:
    return _gws("sheets", "spreadsheets", "values", "get", "--params",
                json.dumps({"spreadsheetId": sid, "range": rng, "valueRenderOption": render})).get("values") or []


def fetch(query: str | None = None) -> tuple[list[dict], list[str]]:
    """(merged points, per-file notes) from every matching spreadsheet."""
    query = query or personal.section("networth_history").get("name_query") or DEFAULT_QUERY
    files = find_files(query)
    if not files:
        raise RuntimeError(f'no spreadsheets named "{query} YYYY" found in Drive')
    per_file, notes = [], []
    for sid, name, year in files:
        tab = balance_tab(sid)
        if not tab:
            notes.append(f"{name}: no balance-sheet tab")
            continue
        ref = "'" + tab.replace("'", "''") + "'"
        grid = _values(sid, f"{ref}!{RANGE}", "UNFORMATTED_VALUE")
        head = _values(sid, f"{ref}!1:1", "FORMULA")
        pts = parse_grid(grid, head[0] if head else None)
        per_file.append((name, year, pts))
        notes.append(f"{name}: {len(pts)} dated column{'s' if len(pts) != 1 else ''}"
                     + (f" ({pts[-1]['date']} .. {pts[0]['date']})" if pts else ""))
    return merge(per_file), notes


def store(con: sqlite3.Connection, points: list[dict], now: str | None = None) -> None:
    """Replace networth_history with points (one transaction)."""
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with con:
        con.execute("DELETE FROM networth_history")
        con.executemany(
            "INSERT INTO networth_history (date, net_worth, assets, liabilities, source, fetched_at) "
            "VALUES (?,?,?,?,?,?)",
            [(p["date"], p["net_worth"], p["assets"], p["liabilities"], p["source"], now) for p in points])


def is_stale(con: sqlite3.Connection, days: int = STALE_DAYS, now: datetime | None = None) -> bool:
    last = con.execute("SELECT MAX(fetched_at) FROM networth_history").fetchone()[0]
    if not last:
        return True
    try:
        then = datetime.fromisoformat(last)
    except ValueError:
        return True
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - then > timedelta(days=days)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--if-stale", action="store_true", help=f"skip unless the last fetch is > {STALE_DAYS} days old")
    args = ap.parse_args(argv)
    con = ctdb.connect(args.db)
    if args.if_stale and not is_stale(con):
        print("net-worth history: fetched within the last week, skipped")
        return 0
    try:
        points, notes = fetch()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired,
            personal.PersonalConfigError) as e:
        print(f"error: net-worth history: {e}", file=sys.stderr)
        return 1
    if not points:
        print("error: net-worth history: no dated Total Equity columns found (sheet layout changed?)", file=sys.stderr)
        return 1
    if args.dry_run:
        for n in notes:
            print(n)
        for p in points:
            print(f"{p['date']}  {p['source']}")
    else:
        store(con, points)
    print(f"net-worth history: {len(points)} points {points[0]['date']} .. {points[-1]['date']}"
          f"{' (dry run, nothing written)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
