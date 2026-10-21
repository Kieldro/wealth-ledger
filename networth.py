#!/usr/bin/env python3
"""Complete net worth = live synced balances + sheet-only items from the "balance sheet" tab.

Each balance-sheet row in SYNCED is replaced by the live balance of the matching account(s).
Every other asset/liability row keeps its manual sheet value (cash, self-custody crypto, metals,
home, cars, unsynced banks/retirement plans…). Synced accounts the sheet has no row for are
added as extra lines. Read-only: never writes the sheet.

The sheet id, the SYNCED map and the asset/liability row labels are the owner's own and live in
personal.json ("sheet_id", "networth"); see personal.py and personal.example.json.

  networth.py            table + totals
  networth.py --json     machine-readable
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import personal

DB = os.path.expanduser("~/.local/share/card-tracker/txns.db")

_NW = personal.section("networth")
SHEET = personal.sheet_id()
# balance-sheet label (column A, exact) -> account_name LIKE patterns in v_accounts (sign as stored)
SYNCED: dict[str, list[str]] = personal.require("networth", "synced")
# rows the sheet's own Total Assets leaves out, mirrored here so the two totals agree
SHEET_EXCLUDED: set[str] = set(_NW.get("sheet_excluded") or [])
# sheet rows that are the real line items (everything else is spot prices, oz counts, ratios)
ASSET_ROWS: set[str] = set(personal.require("networth", "asset_rows"))
LIAB_ROWS: set[str] = set(personal.require("networth", "liab_rows"))
# extra synced accounts with no sheet row (kind custodial is never the owner's money)
EXTRA_KINDS = ("credit", "retirement", "checking", "savings", "investment", "hsa", "loan")


def sheet_rows() -> dict[str, float]:
    out = subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "get", "--params",
         json.dumps({"spreadsheetId": SHEET, "range": "balance sheet!A1:B140",
                     "valueRenderOption": "UNFORMATTED_VALUE"})],
        capture_output=True, text=True, check=True).stdout
    rows = {}
    for r in json.loads(out).get("values", []):
        if len(r) >= 2 and isinstance(r[1], (int, float)) and str(r[0]).strip():
            label = str(r[0]).strip()
            rows.setdefault(label, float(r[1]))  # first occurrence wins
    return rows


def main() -> int:
    sheet = sheet_rows()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    accts = db.execute("SELECT account_id, account_name, balance, kind, as_of FROM v_accounts").fetchall()
    # estimates from valuations/ (metals at spot, crypto wallets, home/car AVMs) replace the sheet's manual numbers
    try:
        vals = {a: (v, src, d) for a, v, src, d in db.execute(
            "SELECT asset, value, source, snapshot_date FROM v_valuations_latest")}
    except sqlite3.OperationalError:  # DB predates the valuations table
        vals = {}
    used_ids, lines = set(), []

    def live(patterns):
        hits = [a for a in accts if any(db.execute("SELECT ? LIKE ?", (a[1], p)).fetchone()[0] for p in patterns)]
        used_ids.update(a[0] for a in hits)
        return sum(a[2] for a in hits) if hits else None

    for label in list(ASSET_ROWS) + list(LIAB_ROWS):
        key = label.rstrip("*")
        if key not in sheet and key not in SYNCED:
            continue
        is_liab = label in LIAB_ROWS
        sval = sheet.get(key)
        lval = live(SYNCED[key]) if key in SYNCED else None
        if lval is not None and is_liab:
            lval = -lval  # liabilities are stored negative; the sheet lists them positive
        value = lval if lval is not None else (sval or 0.0)
        src = "live" if lval is not None else "sheet"
        if lval is None and key in vals:
            lval, vsrc, vdate = vals[key]
            value, src = lval, f"estimate ({vsrc}, {vdate})"
        if key in SHEET_EXCLUDED:
            value, src = 0.0, "sheet, excluded by sheet's own total"
        lines.append({"line": key, "type": "liability" if is_liab else "asset", "sheet": sval,
                      "live": lval, "used": value, "from": src})

    for aid, name, bal, kind, as_of in accts:
        if aid in used_ids or kind not in EXTRA_KINDS or abs(bal or 0) < 0.01:
            continue
        liab = kind in ("credit", "loan")
        lines.append({"line": name, "type": "liability" if liab else "asset", "sheet": None,
                      "live": -bal if liab else bal, "used": -bal if liab else bal, "from": "live (no sheet row)"})

    assets = sum(l["used"] for l in lines if l["type"] == "asset")
    liabs = sum(l["used"] for l in lines if l["type"] == "liability")
    result = {"assets": round(assets, 2), "liabilities": round(liabs, 2), "net_worth": round(assets - liabs, 2),
              "sheet_net_worth": sheet.get("Total Equity"), "lines": lines}
    if "--json" in sys.argv:
        print(json.dumps(result, indent=1))
        return 0
    print(f"{'line':42} {'type':9} {'sheet':>12} {'live':>12} {'used':>12}  from")
    for l in sorted(lines, key=lambda l: (l["type"], -abs(l["used"]))):
        f = lambda v: "" if v is None else f"{v:,.0f}"
        print(f"{l['line'][:42]:42} {l['type']:9} {f(l['sheet']):>12} {f(l['live']):>12} {f(l['used']):>12}  {l['from']}")
    print(f"\nassets {assets:,.0f}  liabilities {liabs:,.0f}  NET WORTH {assets - liabs:,.0f}"
          f"   (sheet's own Total Equity: {result['sheet_net_worth'] or 0:,.0f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
