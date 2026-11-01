"""insights/nw_history.py on synthetic balance-sheet grids (no real data)."""
import sqlite3
from datetime import date, datetime, timedelta, timezone

import ctdb
from insights import nw_history as nh

TODAY = date(2026, 11, 1)


def serial(d: date) -> int:
    return (d - nh.SHEETS_EPOCH).days


#         A          B (live)        C   D                         E           F          G             H                         I
HEADER = ["Assets ", serial(TODAY), "", serial(date(2025, 7, 1)), "4/1/2025", "Current", "13/45/2025", serial(date(2025, 1, 1)),
          serial(date(2024, 10, 1))]
GRID = [
    HEADER,
    ["Checking", 10, "", 9, 8, 7, 6, 5, 4],
    ["Total Assets", 1000, "", 900, 800, 700, 600, 500, "#REF!"],
    ["Total Liabilities ", 100, "", 90, 80, 70, 60, 50, 40],
    ["Total Equity", 900, "", 810, 720, 630, 540, "#REF!", "#REF!"],
    ["Total Equity", -1, "", -1, -1, -1, -1, -1, -1],  # a later row with the same label is ignored
]


def test_header_date_serial_and_strings():
    assert nh.header_date(serial(date(2024, 1, 1))) == date(2024, 1, 1)
    assert nh.header_date("7/18/2025") == date(2025, 7, 18)
    assert nh.header_date("2023-09-01") == date(2023, 9, 1)
    for v in ("Current", "", None, 12.5, True, "13/45/2025"):
        assert nh.header_date(v) is None


def test_col_letter():
    assert [nh.col_letter(i) for i in (0, 3, 25, 26, 51)] == ["A", "D", "Z", "AA", "AZ"]


def test_parse_grid_skips_live_undated_and_error_columns():
    pts = nh.parse_grid(GRID, ["Assets ", "=TODAY()"], today=TODAY)
    # B is the live TODAY() column, C/F/G have no date, I has only error cells for the totals
    assert [(p["date"], p["col"]) for p in pts] == [("2025-07-01", "D"), ("2025-04-01", "E"), ("2025-01-01", "H")]
    d, e, h = pts
    assert (d["net_worth"], d["assets"], d["liabilities"]) == (810.0, 900.0, 90.0)
    assert e["net_worth"] == 720.0
    assert h["net_worth"] == 450.0  # Total Equity is #REF!: assets - liabilities instead


def test_parse_grid_today_column_skipped_without_formula():
    hdr = ["Assets", serial(TODAY), serial(TODAY + timedelta(days=3)), serial(TODAY - timedelta(days=1))]
    g = [hdr, ["Total Equity", 3, 2, 1]]
    assert [(p["col"], p["net_worth"]) for p in nh.parse_grid(g, None, today=TODAY)] == [("D", 1.0)]
    # a TODAY() header is skipped even when its rendered date is in the past (sheet last recalculated earlier)
    assert nh.parse_grid(g, ["Assets", "", "", "=today( )"], today=TODAY + timedelta(days=9)) == [
        {"date": TODAY.isoformat(), "col": "B", "net_worth": 3.0, "assets": None, "liabilities": None},
        {"date": (TODAY + timedelta(days=3)).isoformat(), "col": "C", "net_worth": 2.0, "assets": None,
         "liabilities": None}]
    assert nh.parse_grid([], None) == []


def test_merge_prefers_file_of_same_year():
    p = lambda d, v, c="B": {"date": d, "col": c, "net_worth": v, "assets": None, "liabilities": None}  # noqa: E731
    out = nh.merge([
        ("Statements 2025", 2025, [p("2025-07-01", 1), p("2024-07-01", 2, "E"), p("2024-01-01", 3, "G")]),
        ("Statements 2024", 2024, [p("2024-07-01", 20, "E"), p("2024-01-01", 30, "G")]),
        ("Statements 2023", 2023, [p("2024-01-01", 300, "C"), p("2023-01-01", 4, "F")]),
        ("Statements 2022", 2022, [p("2023-01-01", 40, "D")]),
    ])
    assert [(o["date"], o["net_worth"], o["source"]) for o in out] == [
        ("2023-01-01", 4, "Statements 2023, column F"),
        ("2024-01-01", 30, "Statements 2024, column G"),
        ("2024-07-01", 20, "Statements 2024, column E"),
        ("2025-07-01", 1, "Statements 2025, column B"),
    ]
    # no file of the date's year: the nearest year, then the newer file
    out = nh.merge([("S 2020", 2020, [p("2022-01-01", 1)]), ("S 2021", 2021, [p("2022-01-01", 2)]),
                    ("S 2023", 2023, [p("2022-01-01", 3)])])
    assert out[0]["source"] == "S 2023, column B"


def test_clean_name_and_find_files(monkeypatch):
    assert nh.clean_name("\x7fStatements  2026 ") == "Statements 2026"
    pages = [{"files": [{"id": "a", "name": "\x7fStatements 2025"}, {"id": "b", "name": "Other Statements 2026"}],
              "nextPageToken": "t"},
             {"files": [{"id": "c", "name": "Statements example"}, {"id": "d", "name": "statements 2019"}]}]
    calls = []

    def fake(*args):
        calls.append(args)
        return pages[len(calls) - 1]
    monkeypatch.setattr(nh, "_gws", fake)
    assert nh.find_files("Statements") == [("d", "statements 2019", 2019), ("a", "Statements 2025", 2025)]
    assert '"pageToken": "t"' in calls[1][-1]


def test_fetch_end_to_end_with_fake_gws(monkeypatch):
    monkeypatch.setattr(nh, "find_files", lambda q: [("s1", "Statements 2025", 2025)])
    monkeypatch.setattr(nh, "balance_tab", lambda sid: "balance sheet")
    seen = []

    def values(sid, rng, render):
        seen.append(rng)
        return [HEADER[:5]] if render == "FORMULA" else GRID
    monkeypatch.setattr(nh, "_values", values)
    monkeypatch.setattr(nh, "date", type("D", (date,), {"today": classmethod(lambda c: TODAY)}))
    pts, notes = nh.fetch("Statements")
    assert [p["date"] for p in pts] == ["2025-01-01", "2025-04-01", "2025-07-01"]
    assert seen == ["'balance sheet'!A1:AZ400", "'balance sheet'!1:1"]
    assert notes == ["Statements 2025: 3 dated columns (2025-01-01 .. 2025-07-01)"]


def test_store_replaces_and_staleness(tmp_path):
    con = ctdb.connect(tmp_path / "t.db")
    assert nh.is_stale(con)
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
    nh.store(con, [{"date": "2020-01-01", "net_worth": 1.0, "assets": 2.0, "liabilities": 1.0, "source": "x"}], old)
    assert nh.is_stale(con)
    nh.store(con, [{"date": "2021-01-01", "net_worth": 5.0, "assets": None, "liabilities": None, "source": "y"}])
    assert not nh.is_stale(con)
    assert con.execute("SELECT date, net_worth, source FROM networth_history").fetchall() == [("2021-01-01", 5.0, "y")]


def test_main_error_line(monkeypatch, tmp_path, capsys):
    def boom(query=None):
        raise RuntimeError("gws failed: offline")
    monkeypatch.setattr(nh, "fetch", boom)
    assert nh.main(["--db", str(tmp_path / "t.db")]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert err == ["error: net-worth history: gws failed: offline"]


def test_main_dry_run_writes_nothing(monkeypatch, tmp_path, capsys):
    pts = [{"date": "2024-01-01", "net_worth": 1.0, "assets": None, "liabilities": None, "source": "S 2024, column G"}]
    monkeypatch.setattr(nh, "fetch", lambda query=None: (pts, ["S 2024: 1 dated column"]))
    db = tmp_path / "t.db"
    assert nh.main(["--db", str(db), "--dry-run"]) == 0
    assert "1 points 2024-01-01 .. 2024-01-01 (dry run" in capsys.readouterr().out
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM networth_history").fetchone()[0] == 0
    assert nh.main(["--db", str(db), "--if-stale"]) == 0
    assert nh.main(["--db", str(db), "--if-stale"]) == 0  # fresh now: skipped without fetching
    assert "skipped" in capsys.readouterr().out.splitlines()[-1]
