#!/usr/bin/env python3
"""Copy the latest home valuation into an "estimates" tab of the spreadsheet, with the date it was made.

One row per asset: label, value, the date of the valuation, and the estimates behind it. The balance sheet can then
point at it (e.g. =VLOOKUP("<label>", estimates!A:B, 2, FALSE)) instead of a typed-in number. Only this tab is ever
written; it is created when missing and rewritten only when a row changed. Assets come from personal.json "home"
"asset" (the valuation's balance-sheet label).

  python sheet_estimates.py            apply
  python sheet_estimates.py --dry-run  show the rows without writing
Never sends notifications (owner's rule).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import personal

TAB = "estimates"
HEADER = ["Asset", "Value", "Updated", "Estimates behind it"]
DB = Path(os.environ.get("WEALTH_LEDGER_DB") or Path.home() / ".local/share/wealth-ledger/txns.db")
SOURCE_NAMES = {"comehome_avm": "ComeHome", "cad_2026_market": "county", "zillow_zestimate": "Zillow"}


def _gws(*args: str) -> dict:
    out = subprocess.run(["gws", "sheets", "spreadsheets", *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"gws failed: {(out.stderr or out.stdout).strip()[:200]}")
    return json.loads(out.stdout or "{}")


def source_name(key: str) -> str:
    if key.startswith(("cad_", "tcad_")):
        return "county " + "".join(c for c in key if c.isdigit())
    return SOURCE_NAMES.get(key, key)


def rows(latest: list[tuple[str, float, str, str]]) -> list[list]:
    """[(asset, value, snapshot_date, detail json)] -> sheet rows below the header. Pure."""
    out = []
    for asset, value, day, detail in latest:
        est = (json.loads(detail or "{}").get("estimates") or {})
        how = " · ".join(f"{source_name(k)} ${v:,.0f}" for k, v in est.items() if v is not None)
        out.append([asset, round(value, 2), day, how])
    return out


def comparable(r: list) -> list:
    """A row as the sheet returns it unformatted (numbers as floats, dates as serial days), so equal rows compare
    equal whatever the sheet's locale shows. Pure."""
    out = []
    for c in r:
        if isinstance(c, str) and len(c) == 10 and c[4] == "-" and c[7] == "-":
            try:
                c = (date.fromisoformat(c) - date(1899, 12, 30)).days
            except ValueError:
                pass
        out.append(float(c) if isinstance(c, (int, float)) else c)
    return out


def latest(assets: list[str]) -> list[tuple[str, float, str, str]]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return [r for a in assets for r in con.execute(
            "SELECT asset, value, snapshot_date, detail FROM v_valuations_latest WHERE asset=?", (a,))]
    finally:
        con.close()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    home = personal.section("home").get("asset")
    want = [HEADER] + rows(latest([home] if home else []))
    if len(want) == 1:
        print("sheet estimates: nothing to write")
        return 0
    if "--dry-run" in argv:
        for r in want:
            print(r)
        return 0
    sheet = personal.sheet_id()
    try:
        tabs = _gws("get", "--params", json.dumps({"spreadsheetId": sheet, "fields": "sheets.properties.title"}))
        if TAB not in {s["properties"]["title"] for s in tabs["sheets"]}:
            _gws("batchUpdate", "--params", json.dumps({"spreadsheetId": sheet}),
                 "--json", json.dumps({"requests": [{"addSheet": {"properties": {"title": TAB}}}]}))
            have = []
        else:
            have = _gws("values", "get", "--params", json.dumps({
                "spreadsheetId": sheet, "range": f"{TAB}!A1:D{len(want)}", "valueRenderOption": "UNFORMATTED_VALUE",
                "dateTimeRenderOption": "SERIAL_NUMBER"})).get("values") or []
        if [comparable(r) for r in have] == [comparable(r) for r in want]:
            print("sheet estimates: up to date")
            return 0
        _gws("values", "update", "--params", json.dumps({"spreadsheetId": sheet, "range": f"{TAB}!A1",
                                                          "valueInputOption": "USER_ENTERED"}),
             "--json", json.dumps({"values": want}))
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        print(f"error: sheet estimates: {e}", file=sys.stderr)
        return 1
    print("sheet estimates: " + ", ".join(f"{r[0]}={r[1]:,.0f} ({r[2]})" for r in want[1:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
