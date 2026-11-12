"""Overdraft forecast against a small synthetic ledger (no real data)."""
from datetime import date, timedelta

import pytest

import ctdb
from insights import overdraft

TODAY = date(2026, 10, 27)
CHK, CARD, IDLE = "chk", "card", "idle"


class Ledger:
    def __init__(self, path):
        self.db = ctdb.connect(path)
        self.n = 0

    def account(self, aid, name, kind, balance):
        self.db.execute("INSERT INTO account_kind VALUES (?,?,NULL)", (aid, kind))
        self.db.execute(ctdb.UPSERT_BALANCE, (TODAY.isoformat(), "test", aid, name, "Bank", str(balance), None, "USD",
                                              TODAY.isoformat()))

    def txn(self, aid, d, amount, desc):
        self.n += 1
        self.db.execute(ctdb.upsert_sql("txns"), (f"t{self.n}", aid, aid, "Bank", d.isoformat(), None, f"{amount:.2f}",
                                                  desc, None, None, 0, "USD", "x", "x", "test"))

    def monthly(self, aid, first, months, amount, desc, every=None):
        for i in range(months):
            d = first + timedelta(days=every * i) if every else first.replace(month=first.month + i)
            self.txn(aid, d, amount, desc)

    def forecast(self, today=TODAY):
        self.db.commit()
        return {a["account_id"]: a for a in overdraft.forecast(self.db, today=today)}

    def card(self, balance, activity, pay_rule=True):
        """A checking account that funds a card. `activity` is (date, amount, description) on the card; every
        payment credited to the card is also drafted from checking the next day."""
        self.account(CHK, "Checking", "checking", 9000)
        self.account(CARD, "Visa (2222)", "credit", balance)
        if pay_rule:
            self.db.execute("INSERT INTO txn_rules VALUES (10, '%EPAYMENT%', 'checking', 'out', 'card_payment', NULL)")
        for d, amt, desc in activity:
            self.txn(CARD, d, amt, desc)
            if amt > 0 and "PAYMENT" in desc.upper():
                self.txn(CHK, d + timedelta(days=1), -amt, "DIRECT DEBIT VISA EPAYMENT")

    def autopays(self, today=TODAY):
        return [(e["date"], e["amount"]) for e in self.forecast(today)[CHK]["events"] if "autopay" in e["label"]]


@pytest.fixture
def led(tmp_path):
    return Ledger(tmp_path / "t.db")


def test_rent_overdraws_and_warns_with_date(led):
    led.account(CHK, "Checking (1111)", "checking", 1500)
    led.monthly(CHK, date(2026, 7, 3), 4, -2100, "DIRECT DEBIT LANDLORD RENT (Cash)")
    led.monthly(CHK, date(2026, 7, 13), 4, -90, "DEBIT CARD PURCHASE PHONE CO 071226 AUTHID:123")
    fc = led.forecast()[CHK]
    rent = [e for e in fc["events"] if "LANDLORD" in e["label"]]
    assert [e["date"] for e in rent] == ["2026-11-03"] and rent[0]["amount"] == -2100
    assert fc["first_short"] == "2026-11-03" and fc["low"] == pytest.approx(1500 - 2100 - 90)
    (w,) = overdraft.warnings(list(led.forecast().values()))
    assert w.startswith("Overdraft risk: Checking (1111) has $1,500, projected -$690")
    assert "Move money in before 11/3" in w


def test_payroll_needs_three_and_ignores_bonus(led):
    led.account(CHK, "Checking", "checking", 5000)
    for d in (date(2026, 7, 30), date(2026, 8, 31), date(2026, 9, 29)):
        led.txn(CHK, d, 3750, "DIRECT DEPOSIT ACME PAYROLL")
    led.txn(CHK, date(2026, 8, 24), 2566, "DIRECT DEPOSIT ACME PAYROLL")  # one-off: must not join the series
    for d in (date(2026, 9, 14), date(2026, 10, 14)):  # only two: not trusted yet
        led.txn(CHK, d, 3750, "DIRECT DEPOSIT ACME PAYROLL")
    ev = led.forecast()[CHK]["events"]
    assert [(e["date"], e["amount"]) for e in ev] == [("2026-10-29", 3750)]


def test_late_draft_counts_as_due_today_and_stopped_series_drops(led):
    led.account(CHK, "Checking", "checking", 1000)
    led.monthly(CHK, date(2026, 7, 25), 3, -100, "DIRECT DEBIT GYM")      # next 10/25: 2 days late -> due today
    led.monthly(CHK, date(2026, 5, 5), 3, -100, "DIRECT DEBIT OLD SUB")   # last 7/5: stopped
    ev = led.forecast()[CHK]["events"]
    assert [(e["label"], e["date"]) for e in ev][:1] == [("GYM", TODAY.isoformat())]
    assert not any("OLD SUB" in e["label"] for e in ev)


def test_established_bill_that_missed_a_cycle_stays_as_a_flagged_estimate(led):
    """Rent drafted three months running, then nothing in October (paid another way, or the lease is over: the
    ledger can't say). It is still projected for November, flagged, so a real debit can't vanish silently."""
    led.account(CHK, "Checking (1111)", "checking", 1000)
    led.monthly(CHK, date(2026, 7, 12), 3, -2100, "DIRECT DEBIT LANDLORD RENT")  # 9/12, then no 10/12 debit
    led.monthly(CHK, date(2026, 7, 20), 3, -60, "DIRECT DEBIT POWER CO")       # 9/20 + 31 days: 10/21, late but still due
    fc = led.forecast()[CHK]
    rent = [e for e in fc["events"] if "LANDLORD" in e["label"]]
    assert rent == [{"date": "2026-11-13", "label": "LANDLORD RENT", "amount": -2100, "estimate": True,
                     "note": "missed last cycle", "balance_after": -1100}]
    power = [e for e in fc["events"] if "POWER" in e["label"]]
    assert [e["date"] for e in power] == ["2026-11-21"] and "note" not in power[0]
    (w,) = overdraft.warnings([fc])
    assert "LANDLORD RENT ~$2,100 11/13, missed last cycle" in w


def test_bill_that_missed_two_cycles_or_was_never_established_drops(led):
    led.account(CHK, "Checking", "checking", 1000)
    led.monthly(CHK, date(2026, 6, 12), 3, -2100, "DIRECT DEBIT LANDLORD RENT")  # last 8/12: two cycles missed
    led.monthly(CHK, date(2026, 8, 14), 2, -45, "DIRECT DEBIT TRIAL BOX")        # only twice, then one missed
    assert led.forecast()[CHK]["events"] == []


def test_bill_with_a_known_last_day_is_not_projected_past_it(led):
    led.account(CHK, "Checking", "checking", 5000)
    led.monthly(CHK, date(2026, 7, 3), 4, -2100, "DIRECT DEBIT LANDLORD RENT")   # next 11/3
    led.monthly(CHK, date(2026, 7, 5), 4, -60, "DIRECT DEBIT POWER CO")
    led.db.commit()
    dates = lambda ends: [e["date"] for e in overdraft.forecast(led.db, today=TODAY, ends=ends)[0]["events"]
                          if "LANDLORD" in e["label"]]
    assert dates({}) == ["2026-11-03"]
    assert dates({"%LANDLORD%": "2026-11-03"}) == ["2026-11-03"]   # the last day itself still counts
    assert dates({"%LANDLORD%": "2026-10-31", "_note": "lease"}) == []
    assert dates({"%OTHER TENANT%": "2026-10-31"}) == ["2026-11-03"]
    assert len(led.forecast()[CHK]["events"]) == 2  # personal.example.json's own "ends" matches neither


def test_sweeps_and_idle_accounts_ignored(led):
    led.account(CHK, "Checking", "checking", 800)
    led.account(IDLE, "Biz checking", "checking", 10)
    led.monthly(CHK, date(2026, 7, 10), 4, 500, "REDEMPTION FROM CORE ACCOUNT FDIC")
    led.monthly(CHK, date(2026, 7, 10), 4, -500, "PURCHASE INTO CORE ACCOUNT FDIC")
    fc = led.forecast()
    assert fc[CHK]["events"] == [] and overdraft.warnings(list(fc.values())) == []


def test_card_autopay_sized_from_learned_statement_close(led):
    """Charges on the 1st and 15th, autopay at month end for what was owed mid-month. The next autopay (10/29)
    must be the $900 owed at that close, not the $1,400 owed today."""
    led.account(CHK, "Checking", "checking", 3000)
    led.account(CARD, "Visa (2222)", "credit", -1400)
    led.db.execute("INSERT INTO txn_rules VALUES (10, '%EPAYMENT%', 'checking', 'out', 'card_payment', NULL)")
    for m in (7, 8, 9, 10):
        led.txn(CARD, date(2026, m, 1), -400, "STORE A")
        led.txn(CARD, date(2026, m, 15), -500, "STORE B")
    for d, amt in ((date(2026, 7, 30), 400), (date(2026, 8, 30), 900), (date(2026, 9, 29), 900)):
        led.txn(CARD, d, amt, "AUTOPAY PAYMENT - THANK YOU")
        led.txn(CHK, d + timedelta(days=1), -amt, "DIRECT DEBIT VISA EPAYMENT")
    ev = led.forecast()[CHK]["events"]
    assert [(e["date"], e["label"], e["amount"]) for e in ev] == [("2026-10-29", "Visa (2222) autopay", -900)]


AUTOPAY, MANUAL = "AUTOPAY PAYMENT - THANK YOU", "ONLINE PAYMENT - THANK YOU"


def test_statement_lag_is_the_one_most_autopays_agree_on(led):
    """Statement closes on the 8th, autopay on the 1st. Quiet days after a close make short lags fit some months
    (13 fits August, 12 fits October), so a hand payment mid-cycle must not push the close into the future: the
    10/31 autopay is the $600 statement less that $200, not the $600 owed today."""
    led.card(-600, [
        (date(2026, 7, 8), -300, "STORE"), (date(2026, 7, 20), -200, "STORE"),
        (date(2026, 8, 1), 300, AUTOPAY), (date(2026, 8, 8), -150, "STORE"), (date(2026, 8, 11), -100, "STORE"),
        (date(2026, 9, 1), 350, AUTOPAY), (date(2026, 9, 5), -400, "STORE"), (date(2026, 9, 20), -250, "STORE"),
        (date(2026, 10, 1), 500, AUTOPAY), (date(2026, 10, 6), -350, "STORE"), (date(2026, 10, 12), -120, "STORE"),
        (date(2026, 10, 14), 200, MANUAL), (date(2026, 10, 20), -80, "STORE")])
    led.db.commit()
    led.db.row_factory = overdraft.sqlite3.Row
    card = {"account_id": CARD, "balance": -600}
    pays = [(date(2026, 8, 1), 300), (date(2026, 9, 1), 350), (date(2026, 10, 1), 500)]
    # 22-24 fit all three and 13-21 fit two of them: 21 is the nearest to the usual lag; 12 fits only one
    assert overdraft.statement_lag(led.db, card, pays) == 21
    assert overdraft.statement_lag(led.db, card, pays[1:]) == 22  # with two payments, only what both fit
    assert overdraft.statement_lag(led.db, card, pays + [(date(2026, 10, 14), 200)]) == 21  # an outlier can't outvote
    assert overdraft.statement_lag(led.db, card, [(date(2026, 10, 14), 77)]) == overdraft.STATEMENT_LAG  # fits nothing
    assert led.autopays() == [("2026-10-31", -400)]


def test_statement_lag_allows_for_a_close_that_moves_by_a_day(led):
    """$10 charged every day except a few quiet days after each close. The four autopays fit closes 23-27,
    22-25, 22-27 and 22-27 days earlier: 23-25 fit all four, 22 fits all but one. The close does move by a day
    between cycles, so 22 counts too, and being the later close it is the safer one: the 11/29 autopay includes
    the charge of 11/7."""
    start, today = date(2026, 6, 1), date(2026, 11, 11)
    cycles = [(date(2026, 8, 2), 27, 4), (date(2026, 8, 30), 25, 3), (date(2026, 9, 29), 27, 5), (date(2026, 10, 30), 27, 5)]
    quiet = {pay - timedelta(days=lag) + timedelta(days=i) for pay, lag, n in cycles for i in range(1, n + 1)}
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]
    owed = lambda d: 10 * sum(1 for x in days if x <= d and x not in quiet)
    pays, paid = [], 0
    for pay, lag, _ in cycles:
        pays.append((pay, owed(pay - timedelta(days=lag)) - paid))
        paid += pays[-1][1]
    led.card(-(owed(today) - paid), sorted([(d, -10, "DAILY LUNCH") for d in days if d not in quiet]
                                           + [(d, a, AUTOPAY) for d, a in pays]))
    led.db.commit()
    led.db.row_factory = overdraft.sqlite3.Row
    assert overdraft.statement_lag(led.db, {"account_id": CARD, "balance": -(owed(today) - paid)}, pays) == 22
    assert led.autopays(today) == [("2026-11-29", -(owed(date(2026, 11, 7)) - paid))]
    assert owed(date(2026, 11, 7)) - owed(date(2026, 11, 6)) == 10  # the day a 23-day lag would have left out


def test_returned_autopay_retry_then_issuer_skips_a_cycle(led):
    """Autopay on the 1st for the statement closed on the 6th. October's draft bounces, the retry posts in the next
    cycle and the issuer sends no November draft. Neither the bounce nor the gap may change what comes next."""
    returned = [
        (date(2026, 7, 2), -500, "STORE"), (date(2026, 7, 15), -300, "STORE"),
        (date(2026, 8, 1), 500, AUTOPAY), (date(2026, 8, 3), -400, "STORE"), (date(2026, 8, 20), -200, "STORE"),
        (date(2026, 9, 1), 700, AUTOPAY), (date(2026, 9, 4), -600, "STORE"), (date(2026, 9, 18), -100, "STORE"),
        (date(2026, 10, 1), 800, AUTOPAY), (date(2026, 10, 3), -250, "STORE"),
        (date(2026, 10, 5), -800, "Returned Payment"), (date(2026, 10, 9), 800, "Payment Thank You - 2nd Attempt")]
    led.card(-350, returned)
    # right after the retry: the 10/6 statement was $1,150 with the bounced $800 still in it, and the retry paid that
    assert led.autopays(date(2026, 10, 12)) == [("2026-10-31", -350)]

    later = [(date(2026, 10, 20), -150, "STORE"), (date(2026, 11, 4), -300, "STORE"), (date(2026, 11, 10), -90, "STORE")]
    for d, amt, desc in later:
        led.txn(CARD, d, amt, desc)
    led.db.execute("UPDATE balances SET balance='-890' WHERE account_id=?", (CARD,))
    # 45 days since the last autopay in the series: still expected, sized at the 11/6 statement ($800), not at the
    # $890 owed today, and only once
    assert led.autopays(date(2026, 11, 15)) == [("2026-11-30", -800)]

    led.txn(CARD, date(2026, 12, 1), 800, AUTOPAY)  # it resumes; the gap doesn't break the series
    led.txn(CHK, date(2026, 12, 2), -800, "DIRECT DEBIT VISA EPAYMENT")
    led.db.execute("UPDATE balances SET balance='-90' WHERE account_id=?", (CARD,))
    # next statement is still open: the $90 owed plus 5 days of the card's usual $9 a day until it closes
    assert led.autopays(date(2026, 12, 5)) == [("2026-12-31", -135)]


def test_card_with_nothing_owed_after_a_skipped_cycle_projects_nothing(led):
    led.card(0, [(date(2026, 7, 1), -100, "STORE"), (date(2026, 8, 1), 100, AUTOPAY), (date(2026, 8, 2), -100, "STORE"),
                 (date(2026, 9, 1), 100, AUTOPAY)])
    assert led.autopays() == []                      # 56 days on, nothing owed: no draft
    assert led.autopays(date(2026, 12, 1)) == []     # and after two missed cycles the series is over anyway


def test_open_statement_adds_the_usual_charges_until_it_closes(led):
    """$20 charged every day, statement closes mid-month, autopay 15 days later. On 10/8 the statement that the 10/29
    autopay pays is still open: $480 owed so far plus six more days of charges."""
    start, today = date(2026, 7, 1), date(2026, 10, 8)
    acts = [(start + timedelta(days=i), -20, "DAILY COFFEE") for i in range((today - start).days + 1)]
    acts += [(date(2026, 7, 30), 300, AUTOPAY), (date(2026, 8, 30), 620, AUTOPAY), (date(2026, 9, 29), 600, AUTOPAY)]
    led.card(-480, sorted(acts))
    assert led.autopays(today) == [("2026-10-29", -600)]


def test_rent_that_skipped_one_month_and_resumed_is_projected_as_an_estimate(led):
    led.account(CHK, "Checking", "checking", 5000)
    for d, amt in ((date(2026, 7, 3), -1500), (date(2026, 8, 3), -1540), (date(2026, 10, 2), -1520)):  # no September
        led.txn(CHK, d, amt, "DIRECT DEBIT LANDLORD RENT")
    ev = led.forecast()[CHK]["events"]
    assert [(e["date"], e["amount"], e["estimate"]) for e in ev] == [("2026-11-01", -1540, True)]


def test_skipped_period_only_links_an_established_series_on_its_usual_day():
    d = date
    rent = [(d(2026, 7, 3), -100.0), (d(2026, 8, 3), -100.0)]
    assert overdraft.chains(rent + [(d(2026, 10, 2), -100.0)], skips=1) == [rent + [(d(2026, 10, 2), -100.0)]]
    assert overdraft.chains(rent + [(d(2026, 10, 2), -100.0)]) == [rent]                # off unless asked for
    assert overdraft.chains(rent + [(d(2026, 9, 16), -100.0)], skips=1) == [rent]       # 44 days: not its day
    assert overdraft.chains([rent[0], (d(2026, 9, 2), -100.0)], skips=1) == []          # two points aren't a rhythm
    twice = rent + [(d(2026, 10, 2), -100.0), (d(2026, 12, 2), -100.0)]
    assert overdraft.chains(twice, skips=1) == [twice[:3]]                              # one miss per series
    assert overdraft.skipped(twice[:3]) and not overdraft.skipped(rent)
    assert overdraft._gaps(twice[:3]) == [31, 30]                                       # the miss counts as two periods


def test_pay_uses_the_latest_amount_once_the_last_two_deposits_agree(led):
    """Semi-monthly pay is two monthly series. After a raise and one short check, the last two deposits match:
    both series project that amount, not the smallest of each one's last three."""
    led.account(CHK, "Checking", "checking", 100)
    pay = [(date(2026, 7, 30), 2000), (date(2026, 8, 14), 2000), (date(2026, 8, 31), 2000), (date(2026, 9, 14), 2200),
           (date(2026, 9, 29), 1800), (date(2026, 10, 14), 2200), (date(2026, 10, 26), 2200)]
    for d, amt in pay:
        led.txn(CHK, d, amt, "DIRECT DEPOSIT ACME PAYROLL")
    ev = led.forecast()[CHK]["events"]
    assert [(e["date"], e["amount"]) for e in ev] == [("2026-11-13", 2200), ("2026-11-24", 2200)]


def test_pay_stays_at_the_smallest_recent_amount_while_the_last_two_differ(led):
    led.account(CHK, "Checking", "checking", 100)
    for d, amt in ((date(2026, 7, 30), 2000), (date(2026, 8, 31), 2200), (date(2026, 9, 29), 1800),
                   (date(2026, 10, 26), 2200)):
        led.txn(CHK, d, amt, "DIRECT DEPOSIT ACME PAYROLL")
    ev = led.forecast()[CHK]["events"]
    assert [(e["date"], e["amount"], e["estimate"]) for e in ev] == [("2026-11-24", 1800, True)]


def test_irregular_payouts_are_not_projected_but_named_in_the_warning(led):
    led.account(CHK, "Checking (1111)", "checking", 1500)
    led.monthly(CHK, date(2026, 7, 3), 4, -2100, "DIRECT DEBIT LANDLORD RENT")
    for d, amt in ((date(2026, 9, 12), 500), (date(2026, 9, 20), 100), (date(2026, 10, 2), 300), (date(2026, 10, 8), 400),
                   (date(2026, 10, 17), 250), (date(2026, 10, 23), 250)):
        led.txn(CHK, d, amt, "DIRECT DEPOSIT GIGS INC 1234 PAYOUT")
    led.txn(CHK, date(2026, 10, 1), 5000, "TRANSFERRED FROM BROKERAGE")  # own money moved by hand: not income
    led.txn(CHK, date(2026, 10, 20), 5000, "TRANSFERRED FROM BROKERAGE")
    led.txn(CHK, date(2026, 10, 25), 5000, "TRANSFERRED FROM BROKERAGE")
    fc = led.forecast()[CHK]
    assert all(e["amount"] < 0 for e in fc["events"])
    assert fc["uncounted_income"] == [{"label": "GIGS INC 1234 PAYOUT", "count": 6, "days": 60, "monthly": 900.0}]
    (w,) = overdraft.warnings([fc])
    assert w.startswith("Overdraft risk: Checking (1111) has $1,500, projected -$600 by 11/3")
    assert w.endswith("Move money in before 11/3. Not counted: irregular deposits (GIGS INC 1234 PAYOUT ~$900/mo).")


def test_regular_pay_is_not_listed_as_uncounted(led):
    led.account(CHK, "Checking", "checking", 100)
    led.monthly(CHK, date(2026, 7, 28), 4, 3000, "DIRECT DEPOSIT ACME PAYROLL", every=14)
    led.monthly(CHK, date(2026, 7, 12), 4, -4000, "DIRECT DEBIT LANDLORD RENT")
    fc = led.forecast()[CHK]
    assert fc["uncounted_income"] == [] and "Not counted" not in overdraft.warnings([fc])[0]
