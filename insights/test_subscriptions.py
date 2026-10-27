"""Subscriptions and unusual charges against a small synthetic ledger (no real data)."""
from datetime import date, timedelta

import pytest

import ctdb
from insights import subscriptions

TODAY = date(2026, 10, 27)
CARD, CHK = "card", "chk"


class Ledger:
    def __init__(self, path):
        self.db = ctdb.connect(path)
        self.n = 0
        for aid, kind in ((CARD, "credit"), (CHK, "checking")):
            self.db.execute("INSERT INTO account_kind VALUES (?,?,NULL)", (aid, kind))

    def txn(self, aid, d, amount, desc, payee=None, pending=0):
        self.n += 1
        self.db.execute(ctdb.upsert_sql("txns"), (f"t{self.n}", aid, f"Acct {aid}", "Bank", d.isoformat(), None,
                                                  f"{amount:.2f}", desc, payee, None, pending, "USD", "x", "x", "test"))

    def monthly(self, aid, last, months, amount, desc, payee=None):
        """`months` charges on the same day of month, the last one on `last`."""
        for i in range(months):
            m = last.month - i
            y = last.year + (m - 1) // 12
            self.txn(aid, last.replace(year=y, month=(m - 1) % 12 + 1), amount, desc, payee)

    def report(self):
        self.db.commit()
        return subscriptions.report(self.db, today=TODAY)


@pytest.fixture
def led(tmp_path):
    return Ledger(tmp_path / "t.db")


def subs_by(rep):
    return {s["label"]: s for s in rep["subscriptions"]}


def test_monthly_and_yearly_with_price_increase_and_totals(led):
    led.monthly(CARD, date(2026, 10, 12), 5, -9.99, "STREAMCO*MEMBERSHIP 8005551234", "StreamCo")
    led.monthly(CARD, date(2026, 10, 3), 3, -12.99, "MUSICAPP", "MusicApp")
    led.monthly(CARD, date(2026, 7, 3), 4, -10.99, "MUSICAPP", "MusicApp")  # older, lower price: one series
    led.txn(CARD, date(2025, 3, 1), -99.0, "CLOUD STORAGE ANNUAL", "CloudBox")
    led.txn(CARD, date(2026, 3, 2), -109.0, "CLOUD STORAGE ANNUAL", "CloudBox")
    rep = led.report()
    s = subs_by(rep)
    st = s["StreamCo"]
    assert (st["cadence"], st["last_amount"], st["next_date"], st["monthly"]) == ("monthly", 9.99, "2026-11-12", 9.99)
    assert st["price_change"] is None and not st["new"] and st["category"] == "subscription"
    mu = s["MusicApp"]
    assert (mu["count"], mu["last_amount"], mu["first_date"]) == (7, 12.99, "2026-04-03")
    assert mu["price_change"] is None  # the increase was three charges ago; only the latest step is flagged
    cb = s["CloudBox"]
    assert cb["cadence"] == "yearly" and cb["monthly"] == pytest.approx(109 / 12, abs=0.01)
    assert cb["next_date"] == "2027-03-02" and cb["price_change"]["from"] == 99.0
    assert rep["totals"]["subscription"]["monthly"] == pytest.approx(9.99 + 12.99 + round(109 / 12, 2))
    assert rep["totals"]["subscription"]["yearly"] == pytest.approx(12 * rep["totals"]["subscription"]["monthly"])


def test_latest_price_increase_flagged(led):
    led.monthly(CARD, date(2026, 9, 5), 4, -15.49, "VIDEOSVC", "VideoSvc")
    led.txn(CARD, date(2026, 10, 5), -17.99, "VIDEOSVC", "VideoSvc")
    pc = subs_by(led.report())["VideoSvc"]["price_change"]
    assert pc == {"from": 15.49, "to": 17.99, "pct": 16.1}


def test_frequent_merchants_stopped_and_topups_are_not_subscriptions(led):
    d = date(2026, 5, 1)
    while d < TODAY:  # groceries every 4 days at similar amounts
        led.txn(CARD, d, -40 - (d.day % 5), "GROCER #12 SPRINGFIELD", "Grocer")
        d += timedelta(days=4)
    led.monthly(CARD, date(2026, 6, 15), 4, -8.0, "OLDSUB", "OldSub")  # stopped in June
    for d in ("2026-07-01", "2026-07-03", "2026-07-30", "2026-08-12", "2026-08-29", "2026-09-27", "2026-10-02"):
        led.txn(CARD, date.fromisoformat(d), -20.0, "API CREDITS", "ApiCo")  # prepaid top-ups
    s = subs_by(led.report())
    assert "Grocer" not in s and "OldSub" not in s and "ApiCo" not in s


def test_new_subscription_needs_two_identical_charges(led):
    led.txn(CARD, date(2026, 9, 20), -6.99, "NEWAPP", "NewApp")
    led.txn(CARD, date(2026, 10, 20), -6.99, "NEWAPP", "NewApp")
    led.txn(CARD, date(2026, 9, 1), -30.0, "SALON", "Salon")  # two visits a month apart, different prices
    led.txn(CARD, date(2026, 10, 1), -42.0, "SALON", "Salon")
    s = subs_by(led.report())
    assert s["NewApp"]["new"] and s["NewApp"]["count"] == 2 and "Salon" not in s


def test_bills_and_transfers_to_people_are_tagged_and_totalled_apart(led):
    led.monthly(CHK, date(2026, 10, 14), 4, -120.0, "DIRECT DEBIT SPRINGFIELD ELECTRIC (Cash)")
    led.txn(CHK, date(2026, 8, 14), -95.0, "DIRECT DEBIT SPRINGFIELD ELECTRIC (Cash)")
    led.monthly(CHK, date(2026, 10, 1), 3, -900.0, "Zelle payment to A PERSON", "Zelle Transfer to A Person")
    led.monthly(CHK, date(2026, 10, 3), 3, -2100.0, "DIRECT DEBIT LANDLORD CO (Cash)")
    led.monthly(CARD, date(2026, 10, 9), 3, -20.0, "NEWSPAPER", "Newspaper")
    rep = led.report()
    cats = {s["label"]: s["category"] for s in rep["subscriptions"]}
    assert cats == {"SPRINGFIELD ELECTRIC": "bill", "Zelle Transfer to A Person": "person",
                    "LANDLORD CO": "bill", "Newspaper": "subscription"}
    t = rep["totals"]
    assert t["subscription"]["monthly"] == 20.0 and t["person"]["monthly"] == 900.0
    assert t["bill"]["count"] == 2 and t["all"]["count"] == 4


def test_unusual_fees_duplicates_and_large_charges(led):
    for i in range(6):
        led.txn(CARD, date(2026, 3, 1) + timedelta(days=20 * i), -30.0 - i, "HARDWARE STORE", "Hardware")
    led.txn(CARD, date(2026, 10, 18), -240.0, "HARDWARE STORE", "Hardware")            # large: 7x usual
    led.txn(CARD, date(2026, 10, 10), -64.5, "BISTRO", "Bistro")
    led.txn(CARD, date(2026, 10, 12), -64.5, "BISTRO", "Bistro")                        # duplicate
    for i in range(5):
        led.txn(CARD, date(2026, 6, 1) + timedelta(days=9 * i), -25.0, "TOLL TAG REBILL")
    led.txn(CARD, date(2026, 10, 20), -25.0, "TOLL TAG REBILL")
    led.txn(CARD, date(2026, 10, 21), -25.0, "TOLL TAG REBILL")                         # routine amount: not dup
    led.txn(CARD, date(2026, 10, 8), -12.34, "INTEREST CHARGE ON PURCHASES")
    led.txn(CARD, date(2026, 10, 9), -40.0, "LATE FEE")
    led.txn(CARD, date(2026, 10, 15), -1.23, "FOREIGN TRANSACTION FEE")
    led.txn(CHK, date(2026, 10, 16), -35.0, "OVERDRAFT ITEM FEE")
    led.txn(CARD, date(2026, 9, 1), -29.0, "ANNUAL MEMBERSHIP FEE")                     # outside the window
    for i in range(4):
        led.txn(CHK, date(2026, 6, 1) + timedelta(days=10 * i), -20.0, "VENMO PAYMENT", "Transfer to Venmo")
    led.txn(CHK, date(2026, 10, 20), -400.0, "VENMO PAYMENT", "Transfer to Venmo")      # person: not "large"
    rep = led.report()
    got = [(u["kind"], u["label"], u["amount"]) for u in rep["unusual"]]
    assert got == [("fee", "OVERDRAFT ITEM FEE", -35.0), ("fee", "FOREIGN TRANSACTION FEE", -1.23),
                   ("fee", "LATE FEE", -40.0), ("fee", "INTEREST CHARGE ON PURCHASES", -12.34),
                   ("duplicate", "Bistro", -64.5), ("large", "Hardware", -240.0)]
    fees = {u["fee_type"] for u in rep["unusual"] if u["kind"] == "fee"}
    assert fees == {"overdraft", "foreign", "late", "interest"}
    assert rep["unusual_counts"] == {"fee": 4, "duplicate": 1, "large": 1}
    assert "also 10/10" in rep["unusual"][4]["reason"] and "usual $32" in rep["unusual"][5]["reason"]
    assert not any(s["label"].startswith("INTEREST") for s in rep["subscriptions"])


def test_fee_types():
    assert subscriptions.fee_type("RETURNED PAYMENT FEE")[0] == "returned"
    assert subscriptions.fee_type("Returned Payment") is None  # the bounced-payment reversal itself
    assert subscriptions.fee_type("NSF CHARGE")[0] == "overdraft"
    assert subscriptions.fee_type("INTL TRANSACTION FEE")[0] == "foreign"
    assert subscriptions.fee_type("Example One Subscription Fee") is None  # a subscription, not a bank fee
