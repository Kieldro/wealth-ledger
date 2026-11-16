"""sheet_estimates: the rows written to the estimates tab, and the change check (no sheet access)."""
import json

import sheet_estimates as se


def test_rows_name_each_estimate():
    detail = json.dumps({"estimates": {"comehome_avm": 242672.0, "cad_2026_market": 242000.0, "zillow_zestimate": None}})
    assert se.rows([("Home", 242336.0, "2026-11-15", detail)]) == [
        ["Home", 242336.0, "2026-11-15", "ComeHome $242,672 · county 2026 $242,000"]]


def test_rows_without_detail():
    assert se.rows([("Home", 1.0, "2026-01-01", None)]) == [["Home", 1.0, "2026-01-01", ""]]


def test_unchanged_row_compares_equal_to_what_the_sheet_returns():
    written = ["Home", 242700.0, "2026-11-15", "ComeHome $242,672"]
    read_back = ["Home", 242700, 46341, "ComeHome $242,672"]  # unformatted: int value, serial date
    assert se.comparable(written) == se.comparable(read_back)
    assert se.comparable(["Home", 242800.0, "2026-11-15", ""]) != se.comparable(["Home", 242700, 46341, ""])
