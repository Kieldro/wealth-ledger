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
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import personal

DB = os.path.expanduser("~/.local/share/wealth-ledger/txns.db")

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
# where a crypto quantity can be read from directly (valuations/crypto.py qty sources); "sheet" is typed in
READ_QTY = {"onchain", "exchange-api", "simplefin"}
STALE_DAYS = 7  # a counted account this far behind the freshest one is named in "warnings" (closed? feed broken?)


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


def valuation_from(source: str, snapshot_date: str, today: str) -> str:
    """Where a valuations/ number comes from, for a line's "from": "live reading (...)" when it was read today
    straight from where the money is, else "estimate (...)".

    A reading is an exchange's own USD balance, or a crypto total whose every counted quantity came from a
    blockchain or an exchange API (priced at the day's market price, as a brokerage balance is). It is an
    estimate when anything in it is modelled or typed in: metals at spot times ounces from the sheet, home and
    car value models, a crypto total with any quantity from the sheet. A reading from an earlier day is an
    estimate too, with its date: the provider didn't run or failed today."""
    read = source.startswith("exchange-api")
    if source.startswith("qty: "):
        qty = [q.strip() for q in source[5:].split(";")[0].split("/")]
        read = all(q in READ_QTY for q in qty)
    return f"live reading ({source})" if read and snapshot_date == today else f"estimate ({source}, {snapshot_date})"


def compute(today: str | None = None) -> dict:
    """{assets, liabilities, net_worth, sheet_net_worth, lines, warnings}; each line says where its number came
    from and, as "as_of", the day it is from (the oldest of its accounts; None for a typed-in sheet value). warnings name what may make the total wrong without failing: a synced row that no longer matches any
    account, a counted balance that stopped updating, an account whose feed sent no balance."""
    sheet = sheet_rows()
    today = today or date.today().isoformat()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    accts = db.execute("SELECT account_id, account_name, balance, kind, snapshot_date, as_of FROM v_accounts").fetchall()
    # the day each balance is from: the bank's own date when the feed gives one, else the day the sync saw it
    seen = {a[0]: (a[5] or a[4] or "")[:10] or None for a in accts}
    # estimates from valuations/ (metals at spot, crypto wallets, home/car AVMs) replace the sheet's manual numbers
    try:
        vals = {a: (v, src, d) for a, v, src, d in db.execute(
            "SELECT asset, value, source, snapshot_date FROM v_valuations_latest")}
    except sqlite3.OperationalError:  # DB predates the valuations table
        vals = {}
    used_ids, lines, warnings = set(), [], []

    def live(patterns):
        hits = [a for a in accts if any(db.execute("SELECT ? LIKE ?", (a[1], p)).fetchone()[0] for p in patterns)]
        used_ids.update(a[0] for a in hits)
        dates = [seen[a[0]] for a in hits if seen[a[0]] and abs(a[2] or 0) >= 0.01]
        return (sum(a[2] or 0.0 for a in hits), min(dates, default=None)) if hits else (None, None)

    for label in list(ASSET_ROWS) + list(LIAB_ROWS):
        key = label.rstrip("*")
        if key not in sheet and key not in SYNCED:
            continue
        is_liab = label in LIAB_ROWS
        sval = sheet.get(key)
        lval, as_of = live(SYNCED[key]) if key in SYNCED else (None, None)
        if lval is not None and is_liab:
            lval = -lval  # liabilities are stored negative; the sheet lists them positive
        value = lval if lval is not None else (sval or 0.0)
        src = "live" if lval is not None else "sheet"
        if lval is None and key in SYNCED:
            warnings.append(f"Net worth: no synced account matches the \"{key}\" row any more, so its typed-in sheet "
                            f"value is used. If the account was renamed it is also counted on its own line.")
        synced = lval is not None
        if lval is None and key in vals:
            lval, vsrc, vdate = vals[key]
            value, src, as_of = lval, valuation_from(vsrc, vdate, today), vdate
        if key in SHEET_EXCLUDED:
            value, src, as_of = 0.0, "sheet, excluded by sheet's own total", None
        # "synced" = a SimpleFIN/Plaid account balance, which the daily series in the dashboard trend already has
        lines.append({"line": key, "type": "liability" if is_liab else "asset", "sheet": sval,
                      "live": lval, "used": value, "from": src, "synced": synced, "as_of": as_of})

    for aid, name, bal, kind, snap, _ in accts:
        if aid in used_ids or kind not in EXTRA_KINDS or abs(bal or 0) < 0.01:
            continue
        used_ids.add(aid)
        liab = kind in ("credit", "loan")
        lines.append({"line": name, "type": "liability" if liab else "asset", "sheet": None,
                      "live": -bal if liab else bal, "used": -bal if liab else bal, "from": "live (no sheet row)",
                      "synced": True, "as_of": seen[aid]})

    newest = max((a[4] for a in accts if a[4]), default=None)
    for aid, name, bal, kind, snap, _ in accts:
        if aid not in used_ids:
            continue
        if bal is None:
            warnings.append(f"Net worth: {name} has no balance from its feed and is counted as 0.")
        elif snap and abs(bal) >= 0.01 and (date.fromisoformat(newest) - date.fromisoformat(snap)).days > STALE_DAYS:
            warnings.append(f"Net worth: {name} was last updated {snap} and is still counted at that balance "
                            f"(closed, re-linked under a new name, or its feed stopped?).")

    assets = sum(l["used"] for l in lines if l["type"] == "asset")
    liabs = sum(l["used"] for l in lines if l["type"] == "liability")
    result = {"assets": round(assets, 2), "liabilities": round(liabs, 2), "net_worth": round(assets - liabs, 2),
              "sheet_net_worth": sheet.get("Total Equity"), "lines": lines, "warnings": warnings}
    return result


def main() -> int:
    result = compute()
    lines = result["lines"]
    if "--json" in sys.argv:
        print(json.dumps(result, indent=1))
        return 0
    print(f"{'line':42} {'type':9} {'sheet':>12} {'live':>12} {'used':>12}  from")
    for l in sorted(lines, key=lambda l: (l["type"], -abs(l["used"]))):
        f = lambda v: "" if v is None else f"{v:,.0f}"
        print(f"{l['line'][:42]:42} {l['type']:9} {f(l['sheet']):>12} {f(l['live']):>12} {f(l['used']):>12}  {l['from']}")
    print(f"\nassets {result['assets']:,.0f}  liabilities {result['liabilities']:,.0f}  NET WORTH {result['net_worth']:,.0f}"
          f"   (sheet's own Total Equity: {result['sheet_net_worth'] or 0:,.0f})")
    for w in result["warnings"]:
        print("WARNING: " + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
