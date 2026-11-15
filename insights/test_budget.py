"""Budget vs actual against a synthetic sheet and ledger (no real data)."""
import re
from datetime import date

import pytest

import ctdb
from insights import budget

TODAY = date(2026, 10, 15)  # halfway through a 31-day month: day 15

SHEET = [
    ["", "Expense", "monthly", "yearly"],                 # header: no number in C
    ["Food", "groceries", 600, 7200],
    ["Car", "gasoline", 100, 1200],
    ["Car", "tolls", 50, 600],
    ["Bills", "electricity", 80, 960],
    ["Home", "electricity", 120, 1440],                    # same item name in another category
    ["Home", "rent", 1500, 18000],
    ["Saving", "retirement contribution", 500, 6000],      # saving, not spending
    ["", "childcare", 300, 3600],                          # blank category
    ["Fun", "hobby", 0, 0],                                # zero budget, nothing spent: hidden
    [],
    ["", "Total", 3250, 39000],
    ["", "monthly income", 9000],                          # below the Total row: ignored
]


class Ledger:
    def __init__(self, path):
        self.db = ctdb.connect(path)
        self.n = 0
        for aid, kind in (("chk", "checking"), ("card", "credit")):
            self.db.execute("INSERT INTO account_kind VALUES (?,?,NULL)", (aid, kind))

    def txn(self, d, amount, desc, payee=None, aid="card", pending=0):
        self.n += 1
        self.db.execute(ctdb.upsert_sql("txns"), (f"t{self.n}", aid, aid, "Bank", d.isoformat(), None,
                                                  f"{amount:.2f}", desc, payee, None, pending, "USD", "x", "x",
                                                  "test"))

    def run(self, month="2026-10", rules=None, **kw):
        self.db.commit()
        lines = budget.parse_sheet(SHEET)["lines"]
        rule_list = budget.DEFAULT_RULES if rules is None else rules + budget.DEFAULT_RULES
        return budget.compute(self.db, lines, month, today=TODAY, rule_list=rule_list, **kw)


def cat(res, name):
    return next(c for c in res["categories"] if c["category"] == name)


def line(res, category, item):
    return next(l for l in cat(res, category)["lines"] if l["item"] == item)


@pytest.fixture
def led(tmp_path):
    return Ledger(tmp_path / "t.db")


def test_parse_sheet_stops_at_total_and_skips_headers():
    s = budget.parse_sheet(SHEET)
    assert s["sheet_total"] == 3250
    assert [(l["category"], l["item"]) for l in s["lines"]][:2] == [("Food", "groceries"), ("Car", "gasoline")]
    assert ("Uncategorized", "childcare") in [(l["category"], l["item"]) for l in s["lines"]]
    assert "monthly income" not in [l["item"] for l in s["lines"]] and len(s["lines"]) == 9


def test_parse_sheet_skips_an_income_block_above_the_expense_header():
    top = [["", "Income", "pre tax", "post tax"], ["", "Salary", 120000, 90000], ["", "Rental income", 2000], [],
           ["", "Expense", "monthly", "yearly"]]
    assert budget.parse_sheet(top + SHEET) == budget.parse_sheet(SHEET)


def test_store_vs_its_gas_station_and_unmapped(led):
    led.txn(date(2026, 10, 2), -80.00, "H-E-B #123 000000000SPRINGFIELD", payee="H-E-B")
    led.txn(date(2026, 10, 3), -45.00, "H-E-B GAS CARWASH #6SPRINGFIELD", payee="H-E-B")
    led.txn(date(2026, 10, 4), -20.00, "HCTRA EZ TAG REBILL")
    led.txn(date(2026, 10, 5), -300.00, "AMAZON MKTPL*AB12CD34", payee="Amazon")
    res = led.run()
    assert line(res, "Food", "groceries")["actual"] == 80
    assert line(res, "Car", "gasoline")["actual"] == 45
    assert line(res, "Car", "tolls")["actual"] == 20
    un = cat(res, budget.UNMAPPED)
    assert un["actual"] == 300 and un["budget"] == 0 and un["over"] and un["merchants"][0]["merchant"] == "Amazon"
    assert res["unmapped"] == 300 and res["mapped"] == 145 and res["actual"] == 445


def test_only_posted_spend_in_the_month_counts(led):
    led.txn(date(2026, 10, 6), -50.00, "WHOLE FOODS MARKET")
    led.txn(date(2026, 10, 7), -999.00, "WHOLE FOODS MARKET", pending=1)   # pending
    led.txn(date(2026, 10, 8), 25.00, "WHOLE FOODS MARKET")                # refund, not spend
    led.txn(date(2026, 9, 30), -70.00, "WHOLE FOODS MARKET")               # last month
    led.txn(date(2026, 11, 1), -70.00, "WHOLE FOODS MARKET")               # next month
    assert line(led.run(), "Food", "groceries")["actual"] == 50
    assert line(led.run("2026-09"), "Food", "groceries")["actual"] == 70


def test_personal_rules_first_and_category_qualified_line(led):
    """Owner rules (personal.json budget.rules) beat the defaults; "^home/electricity" picks the second line."""
    own = [(r"EXAMPLE PROPERTY MGMT", r"^home/rent$"), (r"EXAMPLE CITY UTIL", r"^home/electricity"),
           (r"ZELLE TRANSFER TO EXAMPLE PERSON", r"childcare"), (r"ZELLE", r"no such line")]
    led.txn(date(2026, 10, 1), -1500.00, "DIRECT DEBIT EXAMPLE PROPERTY MGMT RENT", aid="chk")
    led.txn(date(2026, 10, 9), -130.00, "EXAMPLE CITY UTILITIES", aid="chk")
    led.txn(date(2026, 10, 10), -300.00, "Zelle payment", payee="Zelle Transfer to Example Person", aid="chk")
    led.txn(date(2026, 10, 11), -40.00, "Zelle payment", payee="Zelle Transfer to Someone Else", aid="chk")
    res = led.run(rules=own)
    assert line(res, "Home", "rent")["actual"] == 1500 and line(res, "Home", "rent")["remaining"] == 0
    assert line(res, "Home", "electricity")["actual"] == 130 and line(res, "Bills", "electricity")["actual"] == 0
    home = cat(res, "Home")
    assert home["budget"] == 1620 and home["actual"] == 1630 and home["over"] and home["remaining"] == -10
    assert line(res, "Uncategorized", "childcare")["actual"] == 300
    assert cat(res, budget.UNMAPPED)["actual"] == 40  # rule matched but its line isn't in the sheet


def test_totals_pace_saving_excluded_and_zero_lines_hidden(led):
    led.txn(date(2026, 10, 2), -400.00, "COSTCO WHSE #0001")
    res = led.run(since="2026-07-20")
    assert res["budget"] == 2750  # sheet lines minus the 500 saving line
    assert [e["item"] for e in res["excluded"]] == ["retirement contribution"]
    assert not any(c["category"] == "Saving" for c in res["categories"])
    assert res["elapsed_days"] == 15 and res["days"] == 31 and res["current"]
    assert res["expected"] == pytest.approx(2750 * 15 / 31, abs=0.01)
    food = cat(res, "Food")
    assert food["expected"] == pytest.approx(600 * 15 / 31, abs=0.01) and food["pct"] == pytest.approx(66.7)
    assert "Fun" not in [c["category"] for c in res["categories"]]
    assert not res["partial_data"] and led.run("2026-07", since="2026-07-20")["partial_data"]
    last = led.run("2026-09")
    assert last["elapsed_days"] == 30 and not last["current"] and last["expected"] == last["budget"]
    assert line(res, "Car", "gasoline")["has_rule"] and not line(res, "Home", "rent")["has_rule"]


def test_default_rules_compile_and_example_config_rules_load():
    budget._compile(budget.DEFAULT_RULES)
    rs = budget.rules()  # personal.example.json via conftest
    assert rs[: 1] == [("EXAMPLE PROPERTY MGMT", "rent")] and rs[-len(budget.DEFAULT_RULES):] == budget.DEFAULT_RULES
    assert budget.exclude_re().search("Saving") and not budget.exclude_re().search("Food")
    assert isinstance(budget.exclude_re(), re.Pattern)


def test_categorizer_gives_each_transaction_its_budget_line():
    lines = [{"category": "food", "item": "groceries", "budget": 400.0},
             {"category": "car", "item": "gasoline", "budget": 80.0},
             {"category": "saving", "item": "groceries fund", "budget": 50.0}]
    cat = budget.categorizer(lines, rule_list=[(r"H-E-B\s*GAS", "gasoline"), (r"H-E-B", "groceries"), (r"NETFLIX", "streaming")])
    assert cat("H-E-B #639 AUSTIN TX", None)["item"] == "groceries"
    assert cat("H-E-B GAS #12", None)["category"] == "car"  # first matching rule wins
    assert cat("NETFLIX.COM", None) is None  # the rule's line is not in the sheet
    assert cat("SOMEWHERE ELSE", "Nobody") is None
