"""insights/mortgage.py: the payoff month from a made-up loan account (balances + payments), no real data."""
from datetime import date

import ctdb
from insights import mortgage


def amortize(balance, payment, rate_m):
    n = 0
    while balance > 0.005:
        balance = balance * (1 + rate_m) - payment
        n += 1
    return n


def test_months_to_pay_matches_paying_it_down_month_by_month():
    for bal, pay, r in ((198997.5, 1500.0, 0.005), (50000.0, 2000.0, 0.004), (1000.0, 1000.0, 0.01), (12000.0, 1000.0, 0.0)):
        assert mortgage.months_to_pay(bal, pay, r) == amortize(bal, pay, r), (bal, pay, r)
    assert mortgage.months_to_pay(100000.0, 400.0, 0.005) is None  # doesn't cover the 500 interest
    assert mortgage.months_to_pay(0.0, 1500.0, 0.005) == 0
    assert mortgage.add_months("2026-11", 106) == "2035-09" and mortgage.add_months("2026-12-17", 1) == "2027-01"


def _ledger(tmp_path, name="MORTGAGE LOAN (1111)", balances=True):
    con = ctdb.connect(tmp_path / "t.db")
    con.execute("INSERT INTO account_kind VALUES ('m', 'loan', NULL)")
    con.execute("INSERT INTO account_kind VALUES ('loc', 'loan', NULL)")  # a paid-off line of credit: not the mortgage
    # 6% a year, 1,400 + 100 extra principal on the 17th: 200,000 -> 199,500 -> 198,997.50
    snaps = [("2026-09-16", -200000.0), ("2026-09-20", -199500.0), ("2026-10-18", -198997.5)] if balances else [("2026-10-18", -198997.5)]
    for d, b in snaps:
        con.execute(ctdb.UPSERT_BALANCE, (d, "simplefin", "m", name, "Lender", f"{b:.2f}", None, "USD", None))
        con.execute(ctdb.UPSERT_BALANCE, (d, "simplefin", "loc", "Line of credit (2)", "Bank", "0.00", None, "USD", None))
    n = 0
    for month in ("2026-08", "2026-09", "2026-10"):
        for amount, desc in ((1400.0, "PAYMENT"), (100.0, "PRINCIPAL PAYMENT")):
            n += 1
            con.execute(ctdb.upsert_sql("txns"), (f"t{n}", "m", name, "Lender", f"{month}-17", None, f"{amount:.2f}", desc,
                                                  None, None, 0, "USD", "x", "x", "simplefin"))
    con.commit()
    return con


def test_payoff_from_the_ledgers_payments_and_balance_drops(tmp_path):
    con = _ledger(tmp_path)
    s = mortgage.summary(con, today=date(2026, 11, 1))
    n = amortize(198997.5, 1500.0, 0.005)
    assert s == {"account": "MORTGAGE LOAN (1111)", "balance": 198997.5, "payment": 1500.0, "rate": 0.06,
                 "rate_from": "balance drops", "last_payment": "2026-10-17", "months": n,
                 "payoff": mortgage.add_months("2026-10", n)}


def test_rate_from_the_name_when_no_month_has_a_balance_on_both_sides(tmp_path):
    con = _ledger(tmp_path, name="Home mortgage 6.5%", balances=False)
    s = mortgage.summary(con, today=date(2026, 11, 1))
    assert s["rate_from"] == "name" and s["rate"] == 0.065 and s["months"] == amortize(198997.5, 1500.0, 0.065 / 12)
    (tmp_path / "plain").mkdir()
    con2 = _ledger(tmp_path / "plain", name="MORTGAGE LOAN (3)", balances=False)
    assert mortgage.summary(con2, today=date(2026, 11, 1), rate_hint="mortgage 7%")["rate"] == 0.07  # the sheet line's name
    assert mortgage.summary(con2, today=date(2026, 11, 1)) is None  # nothing to go on: no line rather than a guess


def test_no_loan_or_no_payments_means_no_payoff(tmp_path):
    con = ctdb.connect(tmp_path / "t.db")
    assert mortgage.summary(con) is None
    con.execute("INSERT INTO account_kind VALUES ('m', 'loan', NULL)")
    con.execute(ctdb.UPSERT_BALANCE, ("2026-10-18", "simplefin", "m", "Mortgage (1)", "L", "-1000.00", None, "USD", None))
    assert mortgage.summary(con) is None
