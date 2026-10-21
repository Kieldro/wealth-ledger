"""Shared SQLite schema for card-tracker (SimpleFIN + Plaid write the same DB).

txns          the real ledger: one row per transaction, `source` says which feed owns it.
              Each account is fed by exactly one source, so nothing is double-counted.
txns_compare  Plaid copies of accounts SimpleFIN already owns (comparison / backfill only).
holdings      daily position snapshots per source (Plaid has cost basis; SimpleFIN reports 0).
balances      daily balance snapshots per account (credit cards/loans are negative = owed).
account_kind  hand-maintained account type (credit/checking/savings/investment/retirement/hsa/loan/crypto);
              new accounts appear in v_accounts with kind NULL until classified.
txn_rules     description LIKE-patterns -> class (first match by priority wins; add rows to teach it).
              Seeded from BASE_RULES below plus the owner's own "rules" in personal.json.
valuations    daily/monthly estimates for things no bank feed covers (metals, crypto wallets, home, cars),
              keyed by the balance-sheet row label they replace in networth.py (see valuations/).
superseded    accounts whose feed was replaced by another source (e.g. a SimpleFIN account later fed by Plaid);
              hidden from every view and no longer written by that feed. History rows are kept.
Views: v_ledger (txns + real date/amount + kind + class), v_balances_latest, v_holdings_latest, v_accounts.

class: spend | income | refund | transfer (between the owner's own accounts) | card_payment | investment.
Spending = class 'spend'; card payments and transfers show up on both sides, so they're never spend.
"""
from __future__ import annotations

import sqlite3

import personal

TXN_COLS = """
  txn_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, account_name TEXT, institution TEXT,
  posted TEXT, transacted_at TEXT, amount TEXT NOT NULL, description TEXT, payee TEXT,
  memo TEXT, pending INTEGER NOT NULL, currency TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'simplefin'
"""
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS txns ({TXN_COLS});
CREATE INDEX IF NOT EXISTS idx_txns_posted ON txns(posted);
CREATE TABLE IF NOT EXISTS txns_compare ({TXN_COLS});
CREATE TABLE IF NOT EXISTS holdings (
  snapshot_date TEXT NOT NULL, source TEXT NOT NULL, account_id TEXT NOT NULL, account_name TEXT,
  institution TEXT, holding_key TEXT NOT NULL, symbol TEXT, description TEXT,
  shares TEXT, price TEXT, market_value TEXT, cost_basis TEXT, currency TEXT,
  PRIMARY KEY (snapshot_date, source, account_id, holding_key)
);
CREATE TABLE IF NOT EXISTS balances (
  snapshot_date TEXT NOT NULL, source TEXT NOT NULL, account_id TEXT NOT NULL, account_name TEXT,
  institution TEXT, balance TEXT, available TEXT, currency TEXT, as_of TEXT,
  PRIMARY KEY (snapshot_date, source, account_id)
);
CREATE TABLE IF NOT EXISTS account_kind (account_id TEXT PRIMARY KEY, kind TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS txn_rules (
  priority INTEGER NOT NULL, pattern TEXT NOT NULL, kind TEXT, direction TEXT, class TEXT NOT NULL, note TEXT,
  UNIQUE (pattern, kind, direction)
);
CREATE TABLE IF NOT EXISTS superseded (account_id TEXT PRIMARY KEY, source TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS valuations (
  snapshot_date TEXT NOT NULL, asset TEXT NOT NULL, value REAL NOT NULL, source TEXT NOT NULL,
  detail TEXT, fetched_at TEXT NOT NULL, PRIMARY KEY (snapshot_date, asset)
);
DROP VIEW IF EXISTS v_valuations_latest;
CREATE VIEW v_valuations_latest AS
  SELECT v.* FROM valuations v
  WHERE v.snapshot_date = (SELECT MAX(snapshot_date) FROM valuations v2 WHERE v2.asset = v.asset);
DROP VIEW IF EXISTS v_ledger;
CREATE VIEW v_ledger AS
  SELECT t.*, COALESCE(t.posted, date(t.transacted_at)) AS date, CAST(t.amount AS REAL) AS amt, k.kind,
    COALESCE(
      (SELECT r.class FROM txn_rules r
       WHERE t.description LIKE r.pattern AND (r.kind IS NULL OR r.kind = k.kind)
         AND (r.direction IS NULL OR r.direction = CASE WHEN CAST(t.amount AS REAL) < 0 THEN 'out' ELSE 'in' END)
       ORDER BY r.priority LIMIT 1),
      CASE WHEN k.kind IN ('retirement','investment','custodial','crypto') THEN 'investment'
           WHEN CAST(t.amount AS REAL) < 0 THEN 'spend'
           WHEN k.kind = 'credit' THEN 'refund'
           ELSE 'income' END) AS class
  FROM txns t LEFT JOIN account_kind k USING (account_id)
  WHERE t.account_id NOT IN (SELECT account_id FROM superseded);
DROP VIEW IF EXISTS v_balances_latest;
CREATE VIEW v_balances_latest AS
  SELECT b.*, CAST(b.balance AS REAL) AS bal, k.kind FROM balances b LEFT JOIN account_kind k USING (account_id)
  WHERE b.snapshot_date = (SELECT MAX(snapshot_date) FROM balances b2
                           WHERE b2.account_id = b.account_id AND b2.source = b.source)
    AND b.account_id NOT IN (SELECT account_id FROM superseded);
DROP VIEW IF EXISTS v_holdings_latest;
CREATE VIEW v_holdings_latest AS
  SELECT h.*, CAST(h.market_value AS REAL) AS value FROM holdings h
  WHERE h.snapshot_date = (SELECT MAX(snapshot_date) FROM holdings h2 WHERE h2.source = h.source)
    AND h.account_id NOT IN (SELECT account_id FROM superseded);
DROP VIEW IF EXISTS v_accounts;
CREATE VIEW v_accounts AS
  SELECT account_id, account_name, institution, source, bal AS balance, as_of, snapshot_date, kind
  FROM v_balances_latest;
"""


def upsert_sql(table: str) -> str:
    return f"""
INSERT INTO {table} (txn_id,account_id,account_name,institution,posted,transacted_at,amount,
                  description,payee,memo,pending,currency,first_seen,last_seen,source)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(txn_id) DO UPDATE SET
  account_name=excluded.account_name, institution=excluded.institution, posted=excluded.posted,
  transacted_at=excluded.transacted_at, amount=excluded.amount, description=excluded.description,
  payee=excluded.payee, memo=excluded.memo, pending=excluded.pending, currency=excluded.currency,
  last_seen=excluded.last_seen, source=excluded.source
"""


UPSERT_BALANCE = """
INSERT OR REPLACE INTO balances (snapshot_date,source,account_id,account_name,institution,balance,available,
                                 currency,as_of)
VALUES (?,?,?,?,?,?,?,?,?)
"""

UPSERT_HOLDING = """
INSERT OR REPLACE INTO holdings (snapshot_date,source,account_id,account_name,institution,holding_key,
                                 symbol,description,shares,price,market_value,cost_basis,currency)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


# (priority, LIKE pattern, account kind or None, 'in'/'out' or None, class, note)
# Generic rules only. Rules naming the owner's own banks, cards, employer or merchants go in personal.json "rules"
# (same 6-field shape) and are seeded by connect() too.
BASE_RULES = [
    # card payments: the checking debit and the card credit are the same money
    (10, "%PAYMENT%", "credit", "in", "card_payment", "payment received on a card"),
    (10, "%Returned Payment%", "credit", "out", "card_payment", "bounced payment reversed"),
    (10, "%PAYMENT%", "loan", "in", "transfer", "mortgage side of the payment (checking side is spend)"),
    # moving money between the owner's own accounts (brokerage feed formats)
    (20, "%CORE ACCOUNT%", None, None, "transfer", "Fidelity cash sweep"),
    (20, "REINVESTMENT%", None, None, "transfer", "money-market reinvest"),
    (20, "%Electronic Funds Transfer%", None, None, "transfer", "Fidelity EFT to own bank"),
    (20, "%TRANSFERRED FROM%", None, None, "transfer", "Fidelity internal transfer"),
    (20, "%MONEYLINE%", None, None, "transfer", "Fidelity MoneyLine pull from own bank"),
    # buying assets is not spending
    (30, "%YOU SOLD%", None, "in", "investment", "sale proceeds"),
    (30, "%DIVIDEND%", None, "in", "income", None),
    (30, "%INTEREST EARNED%", None, "in", "income", None),
    (30, "Interest Paid%", None, "in", "income", None),
    (30, "%Redemption%", "checking", "in", "income", "card rewards redeemed"),
]


def personal_rules() -> list[tuple]:
    """The owner's extra rules from personal.json ("rules": [[priority, pattern, kind, direction, class, note]])."""
    out = []
    for r in personal.load().get("rules") or []:
        if len(r) != 6:
            raise personal.PersonalConfigError(f"personal.json rule needs 6 fields, got {r!r}")
        out.append(tuple(r))
    return out


def connect(path) -> sqlite3.Connection:
    """Open the DB, creating tables and migrating pre-`source` databases in place."""
    db = sqlite3.connect(path)
    cols = {r[1] for r in db.execute("PRAGMA table_info(txns)")}
    if cols and "source" not in cols:
        db.execute("ALTER TABLE txns ADD COLUMN source TEXT NOT NULL DEFAULT 'simplefin'")
    db.executescript(SCHEMA)
    # UNIQUE treats NULLs as distinct, so match with IS to avoid re-adding rules that have no kind/direction
    db.execute("DELETE FROM txn_rules WHERE rowid NOT IN "
               "(SELECT MIN(rowid) FROM txn_rules GROUP BY pattern, kind, direction)")
    db.executemany("INSERT INTO txn_rules (priority,pattern,kind,direction,class,note) SELECT ?,?,?,?,?,? "
                   "WHERE NOT EXISTS (SELECT 1 FROM txn_rules WHERE pattern = ? AND kind IS ? AND direction IS ?)",
                   [(*r, r[1], r[2], r[3]) for r in BASE_RULES + personal_rules()])
    db.commit()
    return db
