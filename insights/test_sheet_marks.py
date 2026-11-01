"""sheet_marks.plan: which balance-sheet labels get the 'typed in by hand' mark (no sheet access)."""
import sheet_marks as sm


def test_marks_manual_rows_and_clears_only_its_own_marks():
    lines = [{"line": "Cash", "from": "sheet"}, {"line": "Checking", "from": "live"},
             {"line": "Total metals value", "from": "estimate (spot)"}, {"line": "Watch", "from": "sheet"}]
    cells = [(0, "Assets", ""), (3, "Cash", ""), (4, "Checking", sm.NOTE),  # became live: unmark
             (5, "Total metals value*", "my own note"),                     # someone else's note: untouched
             (6, "Watch", sm.NOTE)]                                         # already marked: no change
    mark, unmark = sm.plan(lines, cells)
    assert mark == [(3, "Cash")] and unmark == [(4, "Checking")]
