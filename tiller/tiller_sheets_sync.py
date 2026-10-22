#!/usr/bin/env python3
"""Sync Tiller Money "Transactions" sheet rows into a local SQLite DB.

Tiller (Money Feeds) writes bank/card transactions into an ordinary Google
Sheet. This script reads that sheet read-only through the Google Sheets API
using a service account, maps columns BY HEADER NAME (users may reorder or add
columns), and upserts into SQLite keyed on Tiller's "Transaction ID".

Env:
  GOOGLE_APPLICATION_CREDENTIALS  path to service-account JSON key
  TILLER_SPREADSHEET_ID           spreadsheet ID (from the sheet URL)
  TILLER_TAB                      optional tab name (default "Transactions")
  WEALTH_LEDGER_DB                 optional DB path
                                  (default ~/.local/share/wealth-ledger/txns.db)

Usage:
  tiller_sheets_sync.py            # full sync (idempotent)
  tiller_sheets_sync.py --incremental   # only rows with Date Added >= last seen
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Optional

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
DEFAULT_TAB = "Transactions"
DEFAULT_DB = Path("~/.local/share/wealth-ledger/txns.db").expanduser()
SHEETS_EPOCH = date(1899, 12, 30)  # Sheets/Excel serial-date day 0

log = logging.getLogger("tiller_sync")

# normalized field -> sheet header (matched case-insensitively, trimmed)
HEADERS = {
    "txn_id": "Transaction ID",
    "account": "Account",
    "institution": "Institution",
    "date": "Date",
    "amount": "Amount",
    "description": "Description",
    "full_description": "Full Description",
    "category": "Category",
    "date_added": "Date Added",
}
REQUIRED = ("txn_id", "date", "amount")


@dataclass(frozen=True)
class Txn:
    txn_id: str
    account: str
    institution: str
    date: Optional[date]
    amount: Decimal
    description: str
    full_description: str
    category: str
    date_added: Optional[date]


# ---------------------------------------------------------------- parsing

def serial_to_date(value: Any) -> Optional[date]:
    """Sheets serial number (or ISO string fallback) -> date."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return SHEETS_EPOCH + timedelta(days=int(value))  # drop time fraction
    s = str(value).strip()
    try:
        return SHEETS_EPOCH + timedelta(days=int(float(s)))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {value!r}")


def to_decimal(value: Any) -> Decimal:
    """Amount -> Decimal. Goes via str() so floats like 0.1 stay exact."""
    if isinstance(value, bool):
        raise ValueError("bool is not an amount")
    if isinstance(value, (int, float)):
        return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    s = str(value).strip().replace("$", "").replace(",", "")
    if s.startswith("(") and s.endswith(")"):  # accounting negatives
        s = "-" + s[1:-1]
    try:
        return Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"unparseable amount: {value!r}") from e


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def parse_rows(values: list[list[Any]]) -> tuple[list[Txn], int]:
    """Convert a values.get 2D array (header row first) into Txn records.

    Returns (records, skipped_count). Blank rows and rows lacking a
    Transaction ID are skipped; malformed rows are skipped with a warning.
    """
    if not values:
        return [], 0
    header = [_text(h).lower() for h in values[0]]
    idx = {}
    for field, name in HEADERS.items():
        try:
            idx[field] = header.index(name.lower())
        except ValueError:
            idx[field] = None
    missing = [f for f in REQUIRED if idx[f] is None]
    if missing:
        raise ValueError(
            "sheet is missing required column(s): "
            + ", ".join(HEADERS[f] for f in missing)
            + f" (found: {values[0]})"
        )

    def cell(row: list[Any], field: str) -> Any:
        i = idx[field]
        # values.get trims trailing empties, so rows can be short
        return row[i] if i is not None and i < len(row) else ""

    out: list[Txn] = []
    skipped = 0
    for n, row in enumerate(values[1:], start=2):
        if not any(_text(c) for c in row):
            continue  # blank row (not counted as skipped)
        txn_id = _text(cell(row, "txn_id"))
        if not txn_id:
            skipped += 1
            continue
        try:
            out.append(Txn(
                txn_id=txn_id,
                account=_text(cell(row, "account")),
                institution=_text(cell(row, "institution")),
                date=serial_to_date(cell(row, "date")),
                amount=to_decimal(cell(row, "amount")),
                description=_text(cell(row, "description")),
                full_description=_text(cell(row, "full_description")),
                category=_text(cell(row, "category")),
                date_added=serial_to_date(cell(row, "date_added")),
            ))
        except ValueError as e:
            log.warning("row %d skipped: %s", n, e)
            skipped += 1
    return out, skipped


# ------------------------------------------------------------- sheets API

def build_sheets_client(credentials_path: Optional[str] = None):
    """Build a read-only Sheets v4 service from a service-account JSON key."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    path = credentials_path or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not path:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS is not set")
    creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def fetch_values(service, spreadsheet_id: str, tab: str = DEFAULT_TAB) -> list[list[Any]]:
    """One values.get call; numbers/dates come back raw (serials, not text)."""
    resp = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab}'",  # whole tab; quoting handles spaces
        valueRenderOption="UNFORMATTED_VALUE",
        dateTimeRenderOption="SERIAL_NUMBER",
    ).execute()
    return resp.get("values", [])


# ---------------------------------------------------------------- sqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    txn_id           TEXT PRIMARY KEY,
    account          TEXT,
    institution      TEXT,
    date             TEXT,      -- ISO yyyy-mm-dd
    amount           TEXT NOT NULL,  -- Decimal as exact string
    amount_cents     INTEGER NOT NULL,  -- for SQL sums/sorts
    description      TEXT,
    full_description TEXT,
    category         TEXT,
    date_added       TEXT,
    first_seen       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_txn_added ON transactions(date_added);
"""

UPSERT = """
INSERT INTO transactions
 (txn_id, account, institution, date, amount, amount_cents, description,
  full_description, category, date_added)
VALUES (?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(txn_id) DO UPDATE SET
  account=excluded.account, institution=excluded.institution,
  date=excluded.date, amount=excluded.amount,
  amount_cents=excluded.amount_cents, description=excluded.description,
  full_description=excluded.full_description, category=excluded.category,
  date_added=excluded.date_added, updated_at=datetime('now')
WHERE (account, institution, date, amount, description, full_description,
       category, date_added) IS NOT
      (excluded.account, excluded.institution, excluded.date, excluded.amount,
       excluded.description, excluded.full_description, excluded.category,
       excluded.date_added)
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    p = Path(db_path).expanduser()
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.executescript(SCHEMA)
    return conn


def upsert(conn: sqlite3.Connection, txns: Iterable[Txn]) -> int:
    """Upsert; rows that did not change are untouched. Returns rows changed."""
    before = conn.total_changes
    with conn:
        for t in txns:
            conn.execute(UPSERT, (
                t.txn_id, t.account, t.institution,
                t.date.isoformat() if t.date else None,
                str(t.amount), int((t.amount * 100).to_integral_value()),
                t.description, t.full_description, t.category,
                t.date_added.isoformat() if t.date_added else None,
            ))
    return conn.total_changes - before


def last_date_added(conn: sqlite3.Connection) -> Optional[date]:
    r = conn.execute("SELECT MAX(date_added) FROM transactions").fetchone()[0]
    return date.fromisoformat(r) if r else None


def sync(service, spreadsheet_id: str, conn: sqlite3.Connection, *,
         tab: str = DEFAULT_TAB, incremental: bool = False) -> dict:
    """Fetch, normalize, upsert. Incremental keeps rows with Date Added >=
    the newest stored (>= not >, since Date Added has day granularity and a
    fill may land later the same day; the upsert makes re-reads harmless).
    Rows with no Date Added are always kept."""
    txns, skipped = parse_rows(fetch_values(service, spreadsheet_id, tab))
    total = len(txns)
    if incremental:
        cutoff = last_date_added(conn)
        if cutoff:
            txns = [t for t in txns if t.date_added is None or t.date_added >= cutoff]
    changed = upsert(conn, txns)
    return {"read": total, "considered": len(txns), "skipped": skipped, "changed": changed}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--incremental", action="store_true",
                    help="only process rows with Date Added >= newest stored")
    ap.add_argument("--db", default=os.environ.get("WEALTH_LEDGER_DB", str(DEFAULT_DB)))
    ap.add_argument("--tab", default=os.environ.get("TILLER_TAB", DEFAULT_TAB))
    ap.add_argument("--spreadsheet-id", default=os.environ.get("TILLER_SPREADSHEET_ID"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not args.spreadsheet_id:
        ap.error("set TILLER_SPREADSHEET_ID or pass --spreadsheet-id")
    conn = connect(args.db)
    stats = sync(build_sheets_client(), args.spreadsheet_id, conn,
                 tab=args.tab, incremental=args.incremental)
    log.info("sync done: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
