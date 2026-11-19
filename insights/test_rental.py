"""Rental P&L against a small synthetic ledger (no real data)."""
from datetime import date

import pytest

import ctdb
from insights import rental

TODAY = date(2026, 10, 27)
START = "2026-07-15"
CHK, CARD, LOAN = "chk", "card", "loan"
CFG = {"label": "Test house",
       "match": {"rent": ["%PAT TENANT%"], "cleaning": ["%SAM SWEEPER%"], "utilities": ["%EXAMPLE POWER%"],
                 "repairs": [{"pattern": "%HARDWARE BARN%", "kinds": ["credit"]}],
                 "mortgage": [{"pattern": "%EXAMPLE HOME LOANS%ACH%", "direction": "out",
                               "kinds": ["checking", "savings"]}]},
       "exclude": ["%GUESTY GIFT CARD%"]}
MORTGAGE = "DIRECT DEBIT EXAMPLE HOME LOANS ACH (Cash)"


class Ledger:
    def __init__(self, path):
        self.db = ctdb.connect(path)
        self.n = 0

    def account(self, aid, name, kind, balance):
        self.db.execute("INSERT INTO account_kind VALUES (?,?,NULL)", (aid, kind))
        self.snapshot(aid, name, TODAY, balance)

    def snapshot(self, aid, name, d, balance):
        self.db.execute(ctdb.UPSERT_BALANCE, (d.isoformat(), "test", aid, name, "Bank", str(balance), None, "USD",
                                              d.isoformat()))

    def txn(self, aid, d, amount, desc, payee=None, pending=0):
        self.n += 1
        self.db.execute(ctdb.upsert_sql("txns"), (f"t{self.n}", aid, aid, "Bank", d.isoformat(), None,
                                                  f"{amount:.2f}", desc, payee, None, pending, "USD", "x", "x", "test"))

    def summary(self, cfg=CFG):
        self.db.commit()
        return rental.summary(self.db, cfg, START, today=TODAY)


@pytest.fixture
def led(tmp_path):
    led = Ledger(tmp_path / "t.db")
    led.account(CHK, "Checking", "checking", 5000)
    led.account(CARD, "Visa", "credit", -300)
    return led


def month(s, key):
    return next(m for m in s["months"] if m["month"] == key)


def test_matching_rules_and_monthly_totals(led):
    led.txn(CHK, date(2026, 9, 3), 400, "DIRECT DEPOSIT AIRBNB 1234 AIRBNB (Cash)", "Airbnb")
    led.txn(CARD, date(2026, 9, 4), -700, "Airbnb", "Airbnb")                  # own stay: not rental income
    led.txn(CHK, date(2026, 9, 1), 650, "Zelle payment from PAT TENANT to Owner")
    led.txn(CHK, date(2026, 9, 20), -150, "Zelle payment from Owner to PAT  TENANT")  # deposit refund
    led.txn(CHK, date(2026, 9, 10), -80, "Zelle payment from Owner to SAM  SWEEPER")
    led.txn(CARD, date(2026, 9, 12), -95.5, "EXAMPLE POWER COOP")
    led.txn(CARD, date(2026, 9, 14), -25, "Guesty", "Guesty")                    # built-in default
    led.txn(CARD, date(2026, 9, 15), -50, "GUESTY GIFT CARD")                    # personal exclude wins
    led.txn(CARD, date(2026, 9, 16), -40, "HARDWARE BARN #12")
    led.txn(CARD, date(2026, 9, 17), 10, "HARDWARE BARN #12 RETURN")             # refund nets out
    led.txn(CHK, date(2026, 9, 18), -30, "HARDWARE BARN")                        # kinds: credit only
    led.txn(CARD, date(2026, 9, 18), -60, "GROCERY STORE")                       # not rental
    led.txn(CARD, date(2026, 9, 19), -45, "EXAMPLE POWER COOP", pending=1)       # pending skipped
    led.txn(CARD, date(2026, 7, 5), -99, "EXAMPLE POWER COOP")                   # before complete data
    s = led.summary()
    sep = month(s, "2026-09")
    assert sep["income"] == {"bookings": 400, "rent": 500}
    assert sep["expenses"] == {"cleaning": 80, "utilities": 95.5, "software": 25, "repairs": 30}
    assert (sep["income_total"], sep["expense_total"], sep["net"]) == (900, 230.5, 669.5)
    assert sep["principal"] is None and sep["principal_basis"] is None
    assert month(s, "2026-07")["n"] == 0 and month(s, "2026-07")["from_day"] == 15
    assert s["label"] == "Test house" and s["since"] == START
    assert [c["key"] for c in s["expense_categories"]] == ["cleaning", "software", "utilities", "repairs"]
    assert s["rules"]["personal"] == 5 and s["rules"]["exclude"] == 1
    assert s["items"][0]["date"] == "2026-09-20"


def test_defaults_only_without_personal_config(led):
    led.txn(CHK, date(2026, 8, 3), 300, "DIRECT DEPOSIT AIRBNB (Cash)")
    led.txn(CARD, date(2026, 8, 4), -80, "EXAMPLE POWER COOP")
    s = led.summary(cfg={})
    assert month(s, "2026-08")["income_total"] == 300 and month(s, "2026-08")["expense_total"] == 0
    assert s["label"] == "Rental property"
    assert led.summary(cfg={"defaults": False})["periods"]["ytd"]["n"] == 0


def _mortgage(led, snapshots=True):
    led.account(LOAN, "MORTGAGE LOAN", "loan", -99400)
    for m in (8, 9, 10):
        led.txn(CHK, date(2026, m, 15), -2000, MORTGAGE)
        led.txn(LOAN, date(2026, m, 13), 1900, "PAYMENT")
        led.txn(LOAN, date(2026, m, 13), 100, "PRINCIPAL PAYMENT")  # extra principal, labelled on its own
    if snapshots:  # balances only exist from 10/12: October is measured, the rest estimated from it
        for d, owed in ((12, 100000), (13, 100000), (14, 99400), (20, 99400)):
            led.snapshot(LOAN, "MORTGAGE LOAN", date(2026, 10, d), -owed)
    else:
        led.db.execute("DELETE FROM balances WHERE account_id=?", (LOAN,))
        led.snapshot(LOAN, "MORTGAGE LOAN", TODAY, -99400)


def test_mortgage_principal_measured_then_estimated(led):
    _mortgage(led)
    led.txn(CHK, date(2026, 10, 2), 2500, "DIRECT DEPOSIT AIRBNB (Cash)")
    s = led.summary()
    octo, sep = month(s, "2026-10"), month(s, "2026-09")
    assert octo["expenses"] == {"mortgage": 2000}
    assert (octo["principal"], octo["principal_basis"]) == (600, "measured")
    assert (octo["net"], octo["net_ex_principal"]) == (500, 1100)
    # scheduled 500 (600 measured - 100 extra) + that month's labelled 100
    assert (sep["principal"], sep["principal_basis"]) == (600, "estimated")
    t3 = s["periods"]["t3m"]
    assert t3["since"] == "2026-07-28" and t3["principal"] == 1800 and t3["principal_basis"] == "estimated"
    # loan-side credits are transfers on the loan account: the mortgage counts once, from checking
    assert t3["expenses"] == {"mortgage": 6000}


def test_mortgage_is_matched_only_by_your_own_pattern(led):
    _mortgage(led)
    assert not any(r.category == "mortgage" for r in rental.rules({})[1])  # no lender is built in
    assert month(led.summary(cfg={}), "2026-10")["expenses"] == {}
    card = {"match": {"mortgage": [{"pattern": "%EXAMPLE HOME LOANS%", "direction": "out", "kinds": ["credit"]}]}}
    assert month(led.summary(cfg=card), "2026-10")["expenses"] == {}        # paid from checking, not a card
    assert month(led.summary(), "2026-10")["expenses"] == {"mortgage": 2000}


def test_mortgage_without_snapshots_uses_labelled_principal_only(led):
    _mortgage(led, snapshots=False)
    sep = month(led.summary(), "2026-09")
    assert (sep["principal"], sep["principal_basis"]) == (100, "labelled")


def test_mortgage_payment_without_loan_credit_is_partial(led):
    _mortgage(led)
    led.txn(CHK, date(2026, 10, 25), -2000, MORTGAGE)  # no matching loan-side credit
    octo = month(led.summary(), "2026-10")
    assert octo["principal_basis"] == "partial" and octo["principal"] == 600


def test_periods_and_partial_months(led):
    led.txn(CHK, date(2026, 7, 25), 100, "DIRECT DEPOSIT AIRBNB (Cash)")
    led.txn(CHK, date(2026, 7, 30), 200, "DIRECT DEPOSIT AIRBNB (Cash)")
    led.txn(CHK, date(2026, 10, 27), 50, "DIRECT DEPOSIT AIRBNB (Cash)")
    s = led.summary()
    assert [m["month"] for m in s["months"]] == ["2026-07", "2026-08", "2026-09", "2026-10"]
    assert [m["partial"] for m in s["months"]] == [True, False, False, True]
    assert s["months"][-1]["mtd"]
    assert s["periods"]["t3m"]["income_total"] == 250            # 7/28 .. 10/27
    assert s["periods"]["ytd"]["since"] == START and s["periods"]["ytd"]["income_total"] == 350


def test_unknown_category_rejected():
    with pytest.raises(ValueError, match="unknown category"):
        rental.rules({"match": {"rnet": ["%X%"]}})


def test_like_regex():
    rx = rental.like_regex("%PAT_TENANT%")
    assert rx.fullmatch("zelle from pat tenant") and rx.fullmatch("PAT-TENANT") and not rx.fullmatch("PATTENANT")
    assert rental.like_regex("A  B").fullmatch("A B")
