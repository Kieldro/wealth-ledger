#!/usr/bin/env python3
"""Mark the balance-sheet rows nothing keeps up to date, so a stale number is easy to spot on the sheet.

A row whose value networth.py takes from the sheet itself (no synced account, no valuation) gets a light yellow
label cell (column A) and a note saying it is typed in by hand. A row that has since become live loses both. Only
cells this script marked are ever changed: it recognises them by the note's first line, so your own formatting and
notes elsewhere are left alone. Values are never written.

  python sheet_marks.py            apply
  python sheet_marks.py --dry-run  show what would change
Never sends notifications (owner's rule).
"""
from __future__ import annotations

import json
import subprocess
import sys

import networth

TAB = "balance sheet"
MARK = "[wealth-ledger] typed in by hand"
NOTE = MARK + "\nNot synced or estimated by wealth-ledger, so this is only as fresh as the last time you updated it."
YELLOW = {"red": 1.0, "green": 0.95, "blue": 0.8}
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}


def _gws(*args: str) -> dict:
    out = subprocess.run(["gws", "sheets", "spreadsheets", *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"gws failed: {(out.stderr or out.stdout).strip()[:200]}")
    return json.loads(out.stdout)


def label_cells() -> tuple[int, list[tuple[int, str, str]]]:
    """(sheetId, [(row index 0-based, label, current note)]) for column A of the balance-sheet tab."""
    d = _gws("get", "--params", json.dumps({"spreadsheetId": networth.SHEET, "ranges": f"{TAB}!A1:A140",
                                             "includeGridData": True}))
    tab = next(s for s in d["sheets"] if s["properties"]["title"] == TAB)
    rows = tab["data"][0].get("rowData") or []
    cells = []
    for i, r in enumerate(rows):
        v = (r.get("values") or [{}])[0]
        cells.append((i, (v.get("formattedValue") or "").strip(), v.get("note") or ""))
    return tab["properties"]["sheetId"], cells


def plan(lines: list[dict], cells: list[tuple[int, str, str]]) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """(rows to mark, rows to unmark). Pure."""
    manual = {l["line"] for l in lines if l["from"] == "sheet"}
    mark, unmark = [], []
    for i, label, note in cells:
        ours = note.startswith(MARK)
        if label and label.rstrip("*") in manual:
            if not ours:
                mark.append((i, label))
        elif ours:
            unmark.append((i, label))
    return mark, unmark


def _req(sheet_id: int, row: int, color: dict, note: str) -> dict:
    return {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": row, "endRowIndex": row + 1,
                                     "startColumnIndex": 0, "endColumnIndex": 1},
                           "cell": {"note": note, "userEnteredFormat": {"backgroundColor": color}},
                           "fields": "note,userEnteredFormat.backgroundColor"}}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        sheet_id, cells = label_cells()
        mark, unmark = plan(networth.compute()["lines"], cells)
        if mark or unmark:
            if "--dry-run" not in argv:
                reqs = [_req(sheet_id, i, YELLOW, NOTE) for i, _ in mark] + \
                       [_req(sheet_id, i, WHITE, "") for i, _ in unmark]
                _gws("batchUpdate", "--params", json.dumps({"spreadsheetId": networth.SHEET}),
                     "--json", json.dumps({"requests": reqs}))
    except (RuntimeError, OSError, ValueError, StopIteration, subprocess.TimeoutExpired) as e:
        print(f"error: sheet marks: {e}", file=sys.stderr)
        return 1
    verb = "would " if "--dry-run" in argv else ""
    for _, label in mark:
        print(f"{verb}mark: {label}")
    for _, label in unmark:
        print(f"{verb}unmark (now live): {label}")
    print(f"sheet marks: {len(mark)} marked, {len(unmark)} unmarked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
