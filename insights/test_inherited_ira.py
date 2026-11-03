"""Inherited IRA tracker against a small synthetic ledger (no real data)."""
import json
from datetime import date, timedelta

import pytest

import ctdb
from insights import inherited_ira as ira

TODAY = date(2026, 10, 27)
ACCT = "inh"
CFG = {"death_year": 2024, "owner_started_rmds": True, "beneficiary_birth_year": 1980, "growth_rate": 0.05}


class Ledger:
    def __init__(self, path):
        self.db = ctdb.connect(path)
        self.n = 0
        self.db.execute("INSERT INTO account_kind VALUES (?,?,NULL)", (ACCT, "retirement"))
        self.db.execute("INSERT INTO account_kind VALUES (?,?,NULL)", ("roth", "retirement"))

    def balance(self, d, bal, aid=ACCT, name="IRA - BDA (0000)"):
        self.db.execute(ctdb.UPSERT_BALANCE, (d.isoformat(), "test", aid, name, "Broker", str(bal), None, "USD",
                                              d.isoformat()))

    def txn(self, d, amount, desc, aid=ACCT):
        self.n += 1
        self.db.execute(ctdb.upsert_sql("txns"), (f"t{self.n}", aid, aid, "Broker", d.isoformat(), None,
                                                  f"{amount:.2f}", desc, None, None, 0, "USD", "x", "x", "test"))

    def report(self, cfg=CFG, today=TODAY):
        self.db.commit()
        return ira.report(self.db, today=today, raw=dict(cfg), where="personal.json")


@pytest.fixture
def led(tmp_path):
    l = Ledger(tmp_path / "t.db")
    l.balance(TODAY - timedelta(days=40), 98000, aid="roth", name="ROTH IRA (1111)")  # must not be picked
    return l


def test_deadline_plan_and_trend(led):
    for i, b in enumerate((100000, 101000, 99500, 102000)):
        led.balance(date(2026, 9, 1) + timedelta(days=15 * i), b)
    r = led.report()
    assert r["account"]["account_name"] == "IRA - BDA (0000)" and not r["missing"]
    assert r["deadline"] == {"year": 2034, "date": "2034-12-31", "years_left": 8, "distribution_years_left": 9,
                             "passed": False, "first_year": 2025}
    t = r["trend"]
    assert t["since"] == "2026-09-01" and t["change"] == 2000 and t["change_pct"] == 2.0 and t["change_30d"] == 1000
    p = r["plan"]
    assert p["years"] == 9 and p["flat_annual"] == pytest.approx(102000 / 9, abs=0.01)
    # level payment at 5% empties the account exactly in the deadline year
    s = p["schedule"]
    assert [x["year"] for x in s] == list(range(2026, 2035)) and s[-1]["end_balance_growth"] == 0
    assert all(abs(x["growth"] - s[0]["growth"]) < 0.02 for x in s) and p["growth_annual"] > p["flat_annual"]
    assert sum(x["flat"] for x in s) == pytest.approx(102000)


def test_distributions_this_year_count_withholding_and_skip_trades(led):
    led.balance(TODAY, 90000)
    led.txn(date(2026, 3, 2), -8000, "PARTIAL DISTRIBUTION (Cash)")
    led.txn(date(2026, 3, 2), -2000, "FED TAX W/H (Cash)")
    led.txn(date(2025, 12, 20), -5000, "NORMAL DISTRIBUTION (Cash)")      # last year
    led.txn(date(2026, 5, 1), -3000, "YOU BOUGHT EXAMPLE FUND (XYZ) (Cash)")
    led.txn(date(2026, 5, 1), -3000, "PURCHASE INTO CORE ACCOUNT MONEY MARKET (Cash)")
    led.txn(date(2026, 6, 1), -40, "FOREIGN TAX PAID EXAMPLE ADR (Cash)")
    led.txn(date(2026, 7, 1), 2500, "TRANSFERRED FROM VS XXX-XXX000-1 (Cash)")  # money in
    r = led.report()
    ty = r["this_year"]
    assert ty["taken"] == 10000 and [x["amount"] for x in ty["rows"]] == [8000, 2000]
    # even spread counts what's taken: (90k + 10k) / 9 years = 11,111, so 1,111 is left for this year
    assert r["plan"]["flat_annual"] == pytest.approx(11111.11) and r["plan"]["this_year_remaining_flat"] == \
        pytest.approx(1111.11)
    # two years left (deadline 2027): 100k / 2 = 50k, 40k still to take this year
    assert led.report(dict(CFG, death_year=2017))["plan"]["this_year_remaining_flat"] == 40000


def test_rmd_single_life_table(led):
    led.balance(date(2025, 12, 31), 120000)
    led.balance(TODAY, 125000)
    led.txn(date(2026, 2, 1), -1000, "IRA DISTRIBUTION (Cash)")
    r = led.report()["rmd"]
    # born 1980 -> age 45 in 2025 (first year after a 2024 death): Table I = 41.0; 2026 is one year on: 40.0
    assert r["status"] == "required" and r["beneficiary_age_first_year"] == 45 and r["factor"] == 40.0
    assert r["basis_exact"] and r["basis_balance"] == 120000 and r["amount"] == 3000 and r["remaining"] == 2000
    assert ira.rmd_factor(2028, 2024, 1980, None)["factor"] == 38.0
    # an owner younger than the beneficiary: the owner's remaining life expectancy is longer, so it wins
    own = ira.rmd_factor(2026, 2024, 1980, 1990)
    assert own["basis"] == "owner" and own["factor"] == pytest.approx(ira.life_expectancy(34) - 2)


def test_rmd_basis_falls_back_to_settings_then_rough(led):
    led.balance(TODAY, 125000)
    r = led.report(dict(CFG, year_end_balances={"2025": 80000}))["rmd"]
    assert r["basis_balance"] == 80000 and r["amount"] == 2000 and "personal.json" in r["basis_source"]
    r = led.report()["rmd"]
    assert not r["basis_exact"] and r["basis_balance"] == 125000 and r["basis_source"].startswith("rough")


def test_unknown_facts_become_settings_messages(led):
    led.balance(TODAY, 50000)
    r = led.report({})
    keys = {m["key"] for m in r["missing"]}
    assert keys == {"death_year", "owner_started_rmds", "beneficiary_birth_year"}
    assert all("personal.json" in m["msg"] for m in r["missing"])
    assert "deadline" not in r and "plan" not in r and r["this_year"]["taken"] == 0
    # RMD status unknown: still show the conditional estimate, labelled
    r = led.report(dict(CFG, owner_started_rmds=None))
    assert r["rmd"]["status"] == "unknown" and r["rmd"]["note"].startswith("Applies only if")
    assert {m["key"] for m in r["missing"]} == {"owner_started_rmds"}
    r = led.report(dict(CFG, owner_started_rmds=False))
    assert r["rmd"]["status"] == "not_required" and "amount" not in r["rmd"]


def test_bad_values_and_example_config(led):
    led.balance(TODAY, 50000)
    r = led.report({"death_year": "2024", "owner_started_rmds": "yes", "growth_rate": 3})
    assert {m["key"] for m in r["missing"]} == {"death_year", "owner_started_rmds", "beneficiary_birth_year"}
    assert r["settings"]["growth_rate"] == ira.DEFAULT_GROWTH
    raw, where = ira.load_settings()  # conftest points this at personal.example.json
    assert where.endswith("personal.example.json") and isinstance(raw, dict)
    assert json.dumps(raw)  # placeholders only
    assert ira.parse_settings(raw)[1]  # the example leaves the facts unset


def test_deadline_year_and_past_deadline(led):
    led.balance(TODAY, 30000)
    r = led.report(dict(CFG, death_year=2016))  # deadline 2026 = this year
    assert r["deadline"]["years_left"] == 0 and r["plan"]["schedule"] == [
        {"year": 2026, "flat": 30000, "growth": 30000, "end_balance_growth": 0}]
    assert "deadline year" in r["rmd"]["note"]
    r = led.report(dict(CFG, death_year=2014))
    assert r["deadline"]["passed"] and "plan" not in r and "passed" in r["notes"][0]


def test_no_account(tmp_path):
    l = Ledger(tmp_path / "e.db")
    r = l.report()
    assert r["account"] is None and "No inherited IRA account" in r["error"]


def test_life_table_matches_pub_590b_spot_values():
    assert len(ira.SINGLE_LIFE) == 121
    for age, f in ((0, 84.6), (30, 55.3), (42, 43.8), (59, 28.0), (60, 27.1), (73, 16.4), (90, 5.7), (120, 1.0)):
        assert ira.life_expectancy(age) == f
    assert ira.life_expectancy(130) == 1.0


def test_manual_distributions_fill_in_before_synced_history(led):
    led.balance(TODAY, 90000)
    led.txn(date(2026, 8, 1), -1000, "PARTIAL DISTRIBUTION (Cash)")  # synced history starts here
    cfg = dict(CFG, manual_distributions=[
        {"date": "2026-04-15", "amount": 5000},     # before the feed's history: counted
        {"date": "2026-08-01", "amount": 1000},     # the feed has it already: ignored (no double count)
        {"date": "2025-06-01", "amount": 7000},     # another year
        {"date": "bad", "amount": 1}, {"amount": 2}])
    ty = led.report(cfg)["this_year"]
    assert ty["taken"] == 6000 and [r["amount"] for r in ty["rows"]] == [5000, 1000]
    assert "entered by hand" in ty["rows"][0]["description"]
