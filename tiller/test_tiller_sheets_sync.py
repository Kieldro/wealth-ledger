"""Offline tests: the Sheets client is a fake, no network or credentials."""
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import tiller_sheets_sync as ts  # noqa: E402


def serial(d: date) -> int:
    return (d - ts.SHEETS_EPOCH).days


class FakeService:
    """Mimics service.spreadsheets().values().get(...).execute()."""

    def __init__(self, values):
        self.values_payload = values
        self.calls = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, **kw):
        self.calls.append(kw)
        return self

    def execute(self):
        return {"range": "Transactions!A1:Z999", "majorDimension": "ROWS",
                "values": self.values_payload}


# Reordered columns, custom extra "My Note" column, blank row, short row
# (trailing empties trimmed like the real API), float amount noise.
HEADER = ["Transaction ID", "Date", "Full Description", "Amount", "Description",
          "Category", "My Note", "Account", "Account #", "Institution",
          "Month", "Week", "Account ID", "Date Added"]


def payload():
    return [
        HEADER,
        ["tx1", serial(date(2026, 10, 14)), "GROCERYMART SPRINGFIELD ST", -54.32,
         "Grocery Mart", "Groceries", "weekly", "Example Rewards Card", "1234",
         "Example Bank", serial(date(2026, 10, 1)), serial(date(2026, 10, 11)),
         "acc1", serial(date(2026, 10, 15))],
        [],                                   # fully blank row
        ["", "", "", "", "", "", "", "", "", "", "", "", "", ""],  # blank-ish
        ["tx2", serial(date(2026, 10, 15)), "PAYMENT THANK YOU", 1000,
         "Payment", "Credit Card Payment", "", "Other Card", "9999", "Other Issuer"],
        ["tx3", serial(date(2026, 10, 16)) + 0.5, "STREAMING SVC", -15.49,
         "Streaming Svc"],                          # short row: no account/date added
        ["tx4", "not-a-date", "BAD", -1.0, "Bad row"],   # malformed -> skipped
        ["", serial(date(2026, 10, 16)), "NO ID", -2, "No id"],  # no txn id
    ]


def test_parse_normalizes_by_header_name():
    txns, skipped = ts.parse_rows(payload())
    assert [t.txn_id for t in txns] == ["tx1", "tx2", "tx3"]
    assert skipped == 2  # bad date + missing id; blank rows not counted
    t1, t2, t3 = txns
    assert t1 == ts.Txn("tx1", "Example Rewards Card", "Example Bank", date(2026, 10, 14),
                        Decimal("-54.32"), "Grocery Mart", "GROCERYMART SPRINGFIELD ST",
                        "Groceries", date(2026, 10, 15))
    assert t2.amount == Decimal("1000") and t2.date_added is None
    assert t2.institution == "Other Issuer"
    assert t3.date == date(2026, 10, 16)  # time fraction dropped
    assert t3.account == "" and t3.category == ""


def test_decimal_exact():
    assert ts.to_decimal(0.1) == Decimal("0.1")
    assert ts.to_decimal("$1,234.50") == Decimal("1234.50")
    assert ts.to_decimal("(12.00)") == Decimal("-12.00")


def test_missing_required_column_raises():
    with pytest.raises(ValueError, match="Transaction ID"):
        ts.parse_rows([["Date", "Amount"], [1, 2]])


def test_header_match_case_and_space_insensitive():
    txns, _ = ts.parse_rows([[" transaction id ", "DATE", "amount"],
                             ["a", serial(date(2026, 1, 2)), -3]])
    assert txns[0].date == date(2026, 1, 2)


def test_fetch_uses_single_unformatted_call():
    svc = FakeService(payload())
    vals = ts.fetch_values(svc, "SHEETID")
    assert len(svc.calls) == 1
    c = svc.calls[0]
    assert c["spreadsheetId"] == "SHEETID"
    assert c["valueRenderOption"] == "UNFORMATTED_VALUE"
    assert c["dateTimeRenderOption"] == "SERIAL_NUMBER"
    assert vals[0] == HEADER


def test_readonly_scope():
    assert ts.SCOPES == ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def test_upsert_idempotent_and_updates(tmp_path):
    conn = ts.connect(tmp_path / "t.db")
    svc = FakeService(payload())
    s1 = ts.sync(svc, "X", conn)
    assert s1["read"] == 3 and s1["changed"] == 3
    rows = conn.execute("SELECT txn_id, amount, amount_cents, date, category "
                        "FROM transactions ORDER BY txn_id").fetchall()
    assert rows[0] == ("tx1", "-54.32", -5432, "2026-10-14", "Groceries")

    # second run: identical data -> no changes, no duplicates
    s2 = ts.sync(svc, "X", conn)
    assert s2["changed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3

    # user recategorizes tx1 in the sheet -> row updated, still 3 rows
    svc.values_payload[1][5] = "Dining"
    s3 = ts.sync(svc, "X", conn)
    assert s3["changed"] == 1
    assert conn.execute("SELECT category FROM transactions WHERE txn_id='tx1'"
                        ).fetchone()[0] == "Dining"
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3


def test_incremental_mode(tmp_path):
    conn = ts.connect(tmp_path / "t.db")
    data = [["Transaction ID", "Date", "Amount", "Date Added"],
            ["a", serial(date(2026, 10, 1)), -1, serial(date(2026, 10, 2))],
            ["b", serial(date(2026, 10, 3)), -2, serial(date(2026, 10, 5))]]
    svc = FakeService(data)
    assert ts.sync(svc, "X", conn, incremental=True)["considered"] == 2  # empty db
    assert ts.last_date_added(conn) == date(2026, 10, 5)

    data.append(["c", serial(date(2026, 10, 6)), -3, serial(date(2026, 10, 7))])
    s = ts.sync(svc, "X", conn, incremental=True)
    assert s["considered"] == 2          # b (same day as cutoff) and c; a skipped
    assert s["changed"] == 1             # only c is new; b unchanged
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3


def test_empty_sheet():
    assert ts.parse_rows([]) == ([], 0)
