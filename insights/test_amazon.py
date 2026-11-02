"""Amazon export import, charge matching, item categories and the budget split (synthetic data only)."""
import csv
import zipfile
from datetime import date

import pytest

import ctdb
from insights import amazon, budget

ORDER_COLS = ["ASIN", "Billing Address", "Carrier Name & Tracking Number", "Currency", "Department", "Gift Message",
              "Gift Recipient Contact", "Gift Sender Name", "Item Serial Number", "Order Date", "Order ID",
              "Order Status", "Original Quantity", "Payment Method Type", "Product Condition", "Product Name",
              "Purchase Order Number", "Ship Date", "Shipment Item Subtotal", "Shipment Item Subtotal Tax",
              "Shipment Status", "Shipping Address", "Shipping Charge", "Shipping Option", "Total Amount",
              "Total Discounts", "Unit Price", "Unit Price Tax", "Website"]
SECRETS = ["SENTINEL-ADDRESS", "SENTINEL-GIFT", "SENTINEL-RECIPIENT", "SENTINEL-SENDER", "SENTINEL-TRACKING",
           "SENTINEL-SERIAL", "SENTINEL-PO"]


def order_row(oid, asin, odate, sdate, name, total, dept="Not Available", pay="Visa - 1234", status="Closed",
              website="Amazon.com"):
    return {"ASIN": asin, "Billing Address": "SENTINEL-ADDRESS", "Shipping Address": "SENTINEL-ADDRESS",
            "Carrier Name & Tracking Number": "SENTINEL-TRACKING", "Currency": "USD", "Department": dept,
            "Gift Message": "SENTINEL-GIFT", "Gift Recipient Contact": "SENTINEL-RECIPIENT",
            "Gift Sender Name": "SENTINEL-SENDER", "Item Serial Number": "SENTINEL-SERIAL",
            "Order Date": f"{odate}T12:00:00Z", "Order ID": oid, "Order Status": status, "Original Quantity": "1",
            "Payment Method Type": pay, "Product Condition": "New", "Product Name": name,
            "Purchase Order Number": "SENTINEL-PO", "Ship Date": f"{sdate}T12:00:00Z" if sdate else "Not Available",
            "Shipment Item Subtotal": "Not Available", "Shipment Item Subtotal Tax": "Not Available",
            "Shipment Status": "Shipped", "Shipping Charge": "0", "Shipping Option": "std",
            "Total Amount": f"{total:.2f}", "Total Discounts": "0", "Unit Price": f"{total:.2f}",
            "Unit Price Tax": "0", "Website": website}


def write_csv(path, cols, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


REFUND_COLS = ["Creation Date", "Currency", "Direct Debit Refund Amount", "Disbursement Type", "Order ID",
               "Payment Status", "Quantity", "Refund Amount", "Refund Date", "Reversal Amount State",
               "Reversal Reason", "Reversal Status", "Website"]


def refund_row(oid, rdate, amount):
    return {"Creation Date": f"{rdate}T12:00:00Z", "Currency": "USD", "Direct Debit Refund Amount": "0",
            "Disbursement Type": "Refund", "Order ID": oid, "Payment Status": "Completed", "Quantity": "1",
            "Refund Amount": f"{amount:.2f}", "Refund Date": f"{rdate}T12:00:00Z", "Reversal Amount State": "Final",
            "Reversal Reason": "Customer return", "Reversal Status": "Completed", "Website": "Amazon.com"}


def make_export(root, orders, refunds=()):
    write_csv(root / "Your Amazon Orders" / "Order History.csv", ORDER_COLS, orders)
    write_csv(root / "Your Returns & Refunds" / "Refund Details.csv", REFUND_COLS, list(refunds))
    return root


class Ledger:
    def __init__(self, path):
        self.db = ctdb.connect(path)
        self.n = 0
        self.db.execute("INSERT INTO account_kind VALUES ('card','credit',NULL)")
        self.db.execute("INSERT INTO account_kind VALUES ('card2','credit',NULL)")

    def txn(self, d, amount, desc, payee="Amazon", aid="card", name="Example Card (1234)"):
        self.n += 1
        self.db.execute(ctdb.upsert_sql("txns"), (f"t{self.n}", aid, name, "Bank", d, None, f"{amount:.2f}", desc,
                                                  payee, None, 0, "USD", "x", "x", "test"))
        self.db.commit()
        return f"t{self.n}"

    def matches(self):
        return {r[0]: r[1:] for r in self.db.execute("SELECT txn_id, order_id, method, confidence, items "
                                                     "FROM amazon_matches")}


@pytest.fixture
def led(tmp_path):
    return Ledger(tmp_path / "t.db")


def test_import_keeps_only_needed_fields_and_is_idempotent(tmp_path, led):
    ex = make_export(tmp_path / "ex", [
        order_row("100-0000001-0000001", "A1", "2026-03-01", "2026-03-02", "Widget, blue", 10.82, dept="Kitchen"),
        order_row("100-0000001-0000001", "A1", "2026-03-01", "2026-03-04", "Widget, blue", 10.82, dept="Kitchen"),
        order_row("100-0000002-0000002", "B2", "2026-03-05", None, "Gadget", 5.00, status="Cancelled"),
    ], [refund_row("100-0000001-0000001", "2026-03-10", 10.82)])
    assert amazon.import_export(led.db, ex) == {"items": 3, "refunds": 1}
    dump = "\n".join(led.db.iterdump())
    for s in SECRETS:
        assert s not in dump
    rows = led.db.execute("SELECT order_id, asin, line, ship_date, amount, pay_last4, department, status "
                          "FROM amazon_items ORDER BY order_id, line").fetchall()
    assert rows[0] == ("100-0000001-0000001", "A1", 0, "2026-03-02", 10.82, "1234", "Kitchen", "Closed")
    assert rows[1][2] == 1 and rows[1][3] == "2026-03-04"         # same ASIN shipped twice -> two lines
    assert rows[2][6] is None and rows[2][7] == "Cancelled"        # "Not Available" department -> NULL
    # re-import (also from a zip of the same export) upserts the same rows
    zp = tmp_path / "export.zip"
    with zipfile.ZipFile(zp, "w") as z:
        for f in ex.rglob("*.csv"):
            z.write(f, f.relative_to(ex))
    amazon.import_export(led.db, ex)
    amazon.import_export(led.db, zp)
    assert led.db.execute("SELECT COUNT(*) FROM amazon_items").fetchone()[0] == 3
    assert led.db.execute("SELECT COUNT(*) FROM amazon_refunds").fetchone()[0] == 1


def test_money_parsing_quirks():
    assert amazon._money("'-1.5'") == -1.5 and amazon._money("1,234.50") == 1234.5
    assert amazon._money("Not Available") is None
    assert amazon._last4("Gift Certificate/Card and Visa - 9876") == "9876"
    assert amazon._last4("Not Available") is None


def test_charge_kinds():
    assert amazon.charge_kind("AMAZON MKTPL*AB12CD34", "Amazon") == "items"
    assert amazon.charge_kind("Amazon.com*XY98", None) == "items"
    assert amazon.charge_kind("AMZN Mktp US*ZZ11", None) == "items"
    assert amazon.charge_kind("Amazon Prime", "Amazon Prime") == "prime"
    assert amazon.charge_kind("Prime Video *AB12", "Amazon Prime Video") == "prime_video"
    assert amazon.charge_kind("AWS EMEA aws.amazon.co", None) == "aws"
    assert amazon.charge_kind("Kindle Svcs*AB12", None) == "digital"
    assert amazon.charge_kind("EXAMPLE GROCERY #12", None) is None


def test_split_shipments_whole_order_and_subset(tmp_path, led):
    amazon.import_export(led.db, make_export(tmp_path / "ex", [
        # order 1: two shipments, billed separately
        order_row("O1", "A", "2026-04-01", "2026-04-02", "Item A", 10.00),
        order_row("O1", "B", "2026-04-01", "2026-04-02", "Item B", 5.50),
        order_row("O1", "C", "2026-04-01", "2026-04-06", "Item C", 7.25),
        # order 2: two shipments billed as one charge
        order_row("O2", "D", "2026-04-03", "2026-04-03", "Item D", 3.00),
        order_row("O2", "E", "2026-04-03", "2026-04-05", "Item E", 4.00),
        # order 3: one ship date but two charges -> subset sum
        order_row("O3", "F", "2026-04-10", "2026-04-11", "Item F", 20.00),
        order_row("O3", "G", "2026-04-10", "2026-04-11", "Item G", 8.00),
        order_row("O3", "H", "2026-04-10", "2026-04-11", "Item H", 2.00),
        # order 4: paid with another card -> never matched to this card
        order_row("O4", "J", "2026-04-12", "2026-04-12", "Item J", 9.99, pay="Visa - 5555"),
    ]))
    t1 = led.txn("2026-04-03", -15.50, "AMAZON MKTPL*AAA1")
    t2 = led.txn("2026-04-07", -7.25, "AMAZON MKTPL*AAA2")
    t3 = led.txn("2026-04-05", -7.00, "Amazon.com*AAA3")
    t4 = led.txn("2026-04-12", -22.00, "AMAZON MKTPL*AAA4")
    t5 = led.txn("2026-04-12", -8.00, "AMAZON MKTPL*AAA5")
    t6 = led.txn("2026-04-13", -9.99, "AMAZON MKTPL*AAA6")
    t7 = led.txn("2026-05-20", -15.50, "AMAZON MKTPL*AAA7")      # amount fits O1 but far too late
    t8 = led.txn("2026-04-04", -5.40, "Amazon Prime", payee="Amazon Prime")
    res = amazon.match(led.db)
    m = led.matches()
    assert m[t1][:2] == ("O1", "shipment") and sorted(eval(m[t1][3])) == [["A", 0], ["B", 0]]
    assert m[t2][:2] == ("O1", "shipment") and eval(m[t2][3]) == [["C", 0]]
    assert m[t3][:2] == ("O2", "order")
    assert m[t4][:2] == ("O3", "subset") and sorted(eval(m[t4][3])) == [["F", 0], ["H", 0]]
    assert m[t5][:2] == ("O3", "subset") and eval(m[t5][3]) == [["G", 0]]
    assert t6 not in m and t7 not in m and t8 not in m
    assert res["matched"] == {"shipment": 2, "order": 1, "subset": 2}
    amazon.match(led.db)                                          # rebuilding gives the same result
    assert led.matches() == m


def test_one_cent_rounding_and_date_window(tmp_path, led):
    amazon.import_export(led.db, make_export(tmp_path / "ex", [
        order_row("O1", "A", "2026-06-01", "2026-06-01", "Item A", 10.01),
        order_row("O1", "B", "2026-06-01", "2026-06-01", "Item B", 10.01),
    ]))
    ok = led.txn("2026-06-11", -20.01, "AMAZON MKTPL*R1")         # +10 days, one cent off: still a match
    amazon.match(led.db)
    assert ok in led.matches()
    led.db.execute("DELETE FROM txns")
    led.txn("2026-06-12", -20.02, "AMAZON MKTPL*R2")               # +11 days: outside the window
    amazon.match(led.db)
    assert led.matches() == {}


def test_refund_matching(tmp_path, led):
    amazon.import_export(led.db, make_export(tmp_path / "ex", [
        order_row("O1", "A", "2026-07-01", "2026-07-02", "Item A", 30.00),
        order_row("O2", "B", "2026-07-01", "2026-07-02", "Item B", 12.00),
    ], [refund_row("O1", "2026-07-10", 30.00), refund_row("O2", "2026-07-10", 12.00)]))
    led.txn("2026-07-03", -30.00, "AMAZON MKTPL*X1")
    cr = led.txn("2026-07-12", 30.00, "AMAZON MKTPLACE PMTS")
    stray = led.txn("2026-09-01", 12.00, "AMAZON MKTPLACE PMTS")  # too long after the refund
    amazon.match(led.db)
    m = led.matches()
    assert m[cr][:2] == ("O1", "refund") and stray not in m


SHEET = [["Food", "groceries", 600], ["Home", "house repairs", 100], ["Home", "furniture", 50],
         ["Clothing", "clothes", 60], ["Clothing", "shoes", 20], ["Garden", "bee keeping", 30],
         ["Garden", "landscaping", 20], ["Health", "toiletries", 20], ["Tech", "repair devices", 30],
         ["Tech", "new laptop", 40], ["Misc", "air filters", 5], ["Misc", "misc", 100], ["", "Total", 1075]]
LINES = budget.parse_sheet(SHEET)["lines"]


def where(dept, name, rules=None):
    idx = amazon.categorize(dept, name, amazon.resolve_items(LINES, rules or amazon.ITEM_RULES))
    return None if idx is None else LINES[idx]["item"]


def test_item_category_rules():
    assert where("Grocery", "Example Brand Oat Cereal 12oz") == "groceries"
    assert where(None, "Ground Coffee, Medium Roast") == "groceries"
    assert where("Lawn & Patio", "Beehive Frame Feeder, 2 pack") == "bee keeping"   # keyword beats department
    assert where("Lawn & Patio", "Garden Hose 50ft") == "landscaping"
    assert where("Tools & Home Improvement", "Cordless Drill Kit") == "house repairs"
    assert where("Apparel", "Men's Cotton T-Shirt") == "clothes"
    assert where("Shoes", "Trail Runner") == "shoes"
    assert where("Health and Beauty", "Fluoride Toothpaste 3 pack") == "toiletries"
    assert where("Electronics", "USB C Cable 6ft, for MacBook") == "repair devices"
    assert where("Electronics", "Example 2026 Laptop 14-inch with 16GB") == "new laptop"
    assert where(None, "Laptop Stand for Desk") == "furniture"
    assert where(None, "MERV 11 Air Filter 20x25x1") == "air filters"
    assert where("Kitchen", "Dinner Plate Set") == "misc"            # "misc" item, not the Misc/air filters line
    assert where(None, "Pet Water Fountain") is None                  # no pets line in this sheet -> unmapped
    own = [("water fountain", r"^misc/misc$")] + amazon.ITEM_RULES    # personal.json amazon.rules go first
    assert where(None, "Pet Water Fountain", own) == "misc"


def test_item_rules_from_personal_json(monkeypatch):
    monkeypatch.setattr(amazon.personal, "section", lambda n: {"rules": [["EXAMPLE THING", "misc"]]})
    assert amazon.item_rules()[0] == ("EXAMPLE THING", "misc")
    monkeypatch.setattr(amazon.personal, "section", lambda n: {"rules": [["only one field"]]})
    with pytest.raises(amazon.personal.PersonalConfigError):
        amazon.item_rules()


def test_budget_splits_matched_charge_and_preserves_totals(tmp_path, led):
    amazon.import_export(led.db, make_export(tmp_path / "ex", [
        order_row("O1", "A", "2026-10-02", "2026-10-03", "Example Oat Cereal", 30.00, dept="Grocery"),
        order_row("O1", "B", "2026-10-02", "2026-10-03", "Cordless Drill Kit", 60.01, dept="Tools & Hardware"),
        order_row("O1", "C", "2026-10-02", "2026-10-03", "Mystery Object 3000", 10.00),
    ]))
    led.txn("2026-10-04", -100.01, "AMAZON MKTPL*S1")
    led.txn("2026-10-06", -40.00, "AMAZON MKTPL*S2")               # no order: stays "Amazon" in unmapped
    led.txn("2026-10-07", -25.00, "EXAMPLE GROCERY #1")
    amazon.match(led.db)
    res = budget.compute(led.db, LINES, "2026-10", today=date(2026, 10, 15),
                         rule_list=[(r"EXAMPLE GROCERY", "groceries")], amazon_rules=amazon.ITEM_RULES)
    assert res["actual"] == pytest.approx(165.01)                  # every dollar still counted once
    food = next(c for c in res["categories"] if c["category"] == "Food")
    home = next(c for c in res["categories"] if c["category"] == "Home")
    un = next(c for c in res["categories"] if c["category"] == budget.UNMAPPED)
    assert food["actual"] == pytest.approx(55.00)
    assert home["actual"] == pytest.approx(60.01)
    assert un["actual"] == pytest.approx(50.00)
    names = {m["merchant"] for m in un["merchants"]}
    assert "Amazon: Mystery Object 3000" in names and "Amazon" in names
    assert food["lines"][0]["merchants"][0]["merchant"] in ("Amazon: Example Oat Cereal", "Example Grocery")


def test_budget_without_amazon_tables(tmp_path):
    """A read-only connection to a database that predates the Amazon tables still computes."""
    import sqlite3
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript("CREATE TABLE t (txn_id, date, amt, description, payee, class, pending);"
                      "CREATE VIEW v_ledger AS SELECT * FROM t;"
                      "INSERT INTO t VALUES ('x','2026-10-03',-5.0,'AMAZON MKTPL*Q','Amazon','spend',0);")
    con.commit()
    ro = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    res = budget.compute(ro, LINES, "2026-10", today=date(2026, 10, 15), rule_list=[], amazon_rules=[])
    assert res["unmapped"] == 5.0


def test_points_match_below_total_and_budget_uses_actual_charge(tmp_path, led):
    amazon.import_export(led.db, make_export(tmp_path / "ex", [
        order_row("O1", "A", "2026-10-02", "2026-10-03", "Example Oat Cereal", 30.00, dept="Grocery"),
        order_row("O1", "B", "2026-10-02", "2026-10-03", "Cordless Drill Kit", 70.00, dept="Tools & Hardware"),
    ]))
    t = led.txn("2026-10-04", -92.00, "AMAZON MKTPL*P1")              # $8.00 (8%) paid with rewards points
    res = amazon.match(led.db)
    assert led.matches()[t][:2] == ("O1", "points")
    assert led.db.execute("SELECT discount FROM amazon_matches WHERE txn_id=?", (t,)).fetchone()[0] == 8.00
    assert res["matched"] == {"points": 1} and res["points_discount_usd"] == 8.00
    b = budget.compute(led.db, LINES, "2026-10", today=date(2026, 10, 15), rule_list=[],
                       amazon_rules=amazon.ITEM_RULES)
    assert b["actual"] == pytest.approx(92.00)                       # the card charge, not the order total
    food = next(c for c in b["categories"] if c["category"] == "Food")
    home = next(c for c in b["categories"] if c["category"] == "Home")
    assert food["actual"] == pytest.approx(27.60) and home["actual"] == pytest.approx(64.40)  # discount pro rata
    rep = amazon.report(led.db, "2026-01-01", None, None)
    assert rep["points_matched"] == 1 and rep["points_discount_usd"] == 8.00 and rep["unmatched"] == 0


def test_points_pass_leaves_ambiguous_pairs_alone(tmp_path, led):
    amazon.import_export(led.db, make_export(tmp_path / "ex", [
        order_row("O1", "A", "2026-05-01", "2026-05-02", "Item A", 50.00),
        order_row("O2", "B", "2026-05-01", "2026-05-02", "Item B", 52.00),
        order_row("O3", "C", "2026-08-01", "2026-08-02", "Item C", 40.00),
    ]))
    two_groups = led.txn("2026-05-03", -47.00, "AMAZON MKTPL*Q1")     # fits O1 and O2 -> neither
    c1 = led.txn("2026-08-03", -37.00, "AMAZON MKTPL*Q2")             # two charges fit O3 -> neither
    c2 = led.txn("2026-08-04", -38.00, "AMAZON MKTPL*Q3")
    amazon.match(led.db)
    m = led.matches()
    assert two_groups not in m and c1 not in m and c2 not in m


def test_exact_match_preferred_and_points_only_use_open_items(tmp_path, led):
    amazon.import_export(led.db, make_export(tmp_path / "ex", [
        order_row("O1", "A", "2026-06-01", "2026-06-02", "Item A", 30.00),
        order_row("O2", "B", "2026-06-01", "2026-06-02", "Item B", 32.00),
    ]))
    exact = led.txn("2026-06-03", -30.00, "AMAZON MKTPL*E1")          # exact for O1, also within the band of O2
    pts = led.txn("2026-06-04", -28.00, "AMAZON MKTPL*E2")            # O1 is taken, so only O2 is open
    amazon.match(led.db)
    m = led.matches()
    assert m[exact][:2] == ("O1", "shipment")
    assert m[pts][:2] == ("O2", "points")


def test_points_pass_never_matches_above_total_or_outside_band(tmp_path, led):
    amazon.import_export(led.db, make_export(tmp_path / "ex", [
        order_row("O1", "A", "2026-07-01", "2026-07-02", "Item A", 20.00),
        order_row("O2", "B", "2026-09-01", "2026-09-02", "Item B", 100.00),
        order_row("O3", "C", "2026-09-20", "2026-09-21", "Item C", 10.00),
    ]))
    above = led.txn("2026-07-03", -21.00, "AMAZON MKTPL*U1")          # more than the order: never a points match
    deep = led.txn("2026-09-03", -80.00, "AMAZON MKTPL*U2")           # 20% below: outside the default band
    late = led.txn("2026-10-15", -9.50, "AMAZON MKTPL*U3")            # in the band but outside the date window
    amazon.match(led.db)
    m = led.matches()
    assert above not in m and deep not in m and late not in m


def test_points_band_config_and_discount_column_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(amazon.personal, "section", lambda n: {"points_band": [0.0, 0.25]})
    assert amazon.points_band() == (0.0, 0.25)
    monkeypatch.setattr(amazon.personal, "section", lambda n: {"points_band": [0.3, 0.1]})
    with pytest.raises(amazon.personal.PersonalConfigError):
        amazon.points_band()
    monkeypatch.setattr(amazon.personal, "section", lambda n: {})
    assert amazon.points_band() == amazon.POINTS_BAND
    import sqlite3
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE amazon_matches (txn_id TEXT PRIMARY KEY, order_id TEXT, method TEXT NOT NULL, "
                "confidence REAL, items TEXT)")
    con.execute("INSERT INTO amazon_matches VALUES ('t1','O1','shipment',0.9,'[]')")
    con.commit()
    con.close()
    db = ctdb.connect(p)
    assert db.execute("SELECT txn_id, discount FROM amazon_matches").fetchall() == [("t1", None)]
