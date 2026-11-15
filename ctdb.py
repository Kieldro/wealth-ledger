"""Shared SQLite schema for wealth-ledger (SimpleFIN + Plaid write the same DB).

txns          the real ledger: one row per transaction, `source` says which feed owns it.
              Each account is fed by exactly one source, so nothing is double-counted.
txns_compare  Plaid copies of accounts SimpleFIN already owns (comparison / backfill only);
              holdings_compare is the same for positions. Neither feeds any view.
holdings      daily position snapshots per source (Plaid has cost basis; SimpleFIN reports 0).
balances      daily balance snapshots per account (credit cards/loans are negative = owed). credit_limit is the
              card's limit when the feed reports one (Plaid balances.limit; SimpleFIN has no such field).
credit_limits manual card limits from personal.json "credit_limits" (account_name LIKE pattern -> limit), rebuilt on
              every connect(); v_accounts.credit_limit falls back to them when the feed has no limit.
account_kind  hand-maintained account type (credit/checking/savings/investment/retirement/hsa/loan/crypto);
              new accounts appear in v_accounts with kind NULL until classified.
txn_rules     description LIKE-patterns -> class (first match by priority wins; add rows to teach it).
              Seeded from BASE_RULES below plus the owner's own "rules" in personal.json.
valuations    daily/monthly estimates for things no bank feed covers (metals, crypto wallets, home, cars),
              keyed by the balance-sheet row label they replace in networth.py (see valuations/).
networth_history  past whole-net-worth points (Total Equity per dated balance-sheet column of the yearly
              spreadsheets), from insights/nw_history.py; the dashboard trend shows them before the daily series.
prices        daily USD closes of BTC (per coin) and XAU (per troy oz of gold), from insights/prices.py; the
              dashboard trend uses them to price net worth in bitcoin and in gold.
amazon_items / amazon_refunds  items and refunds from an Amazon "Your Orders" data export (insights/amazon.py;
              no addresses, gift details or tracking). amazon_matches links an Amazon card charge (txn_id) to the
              order items it paid for (items = JSON [[asin, line], ...]; discount = rewards points that paid the
              rest, for method 'points'); insights/budget.py splits it by item.
superseded    accounts whose feed was replaced by another source (e.g. a SimpleFIN account later fed by Plaid);
              hidden from every view and no longer written by that feed. History rows are kept.
Views: v_ledger (txns + real date/amount + kind + class), v_balances_latest, v_holdings_latest,
v_accounts (latest balance per account + credit_limit, from the feed or else personal.json, and credit_limit_source).

class: spend | income | refund | transfer (between the owner's own accounts) | card_payment | investment.
Spending = class 'spend'; card payments and transfers show up on both sides, so they're never spend.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

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
CREATE TABLE IF NOT EXISTS holdings_compare (
  snapshot_date TEXT NOT NULL, source TEXT NOT NULL, account_id TEXT NOT NULL, account_name TEXT,
  institution TEXT, holding_key TEXT NOT NULL, symbol TEXT, description TEXT,
  shares TEXT, price TEXT, market_value TEXT, cost_basis TEXT, currency TEXT,
  PRIMARY KEY (snapshot_date, source, account_id, holding_key)
);
CREATE TABLE IF NOT EXISTS balances (
  snapshot_date TEXT NOT NULL, source TEXT NOT NULL, account_id TEXT NOT NULL, account_name TEXT,
  institution TEXT, balance TEXT, available TEXT, currency TEXT, as_of TEXT, credit_limit TEXT,
  PRIMARY KEY (snapshot_date, source, account_id)
);
CREATE TABLE IF NOT EXISTS credit_limits (pattern TEXT PRIMARY KEY, credit_limit REAL NOT NULL);
CREATE TABLE IF NOT EXISTS account_kind (account_id TEXT PRIMARY KEY, kind TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS txn_rules (
  priority INTEGER NOT NULL, pattern TEXT NOT NULL, kind TEXT, direction TEXT, class TEXT NOT NULL, note TEXT,
  UNIQUE (pattern, kind, direction)
);
CREATE TABLE IF NOT EXISTS superseded (account_id TEXT PRIMARY KEY, source TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS late_duplicates (txn_id TEXT PRIMARY KEY, dup_of TEXT NOT NULL, reason TEXT, found_at TEXT);
CREATE TABLE IF NOT EXISTS valuations (
  snapshot_date TEXT NOT NULL, asset TEXT NOT NULL, value REAL NOT NULL, source TEXT NOT NULL,
  detail TEXT, fetched_at TEXT NOT NULL, PRIMARY KEY (snapshot_date, asset)
);
CREATE TABLE IF NOT EXISTS networth_history (
  date TEXT PRIMARY KEY, net_worth REAL, assets REAL, liabilities REAL, source TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS prices (
  date TEXT NOT NULL, symbol TEXT NOT NULL, usd REAL NOT NULL, source TEXT, PRIMARY KEY (date, symbol)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- prices_updated: when the closes were last fetched (UTC)
CREATE TABLE IF NOT EXISTS crypto_transfers (
  wallet TEXT NOT NULL, coin TEXT NOT NULL, txid TEXT NOT NULL, direction TEXT NOT NULL, date TEXT NOT NULL,
  amount REAL NOT NULL, fee REAL, address TEXT, counterparty TEXT, memo TEXT, source TEXT NOT NULL,
  PRIMARY KEY (wallet, coin, txid, direction)
);
CREATE TABLE IF NOT EXISTS amazon_items (
  order_id TEXT NOT NULL, asin TEXT NOT NULL, line INTEGER NOT NULL, order_date TEXT, ship_date TEXT, name TEXT,
  department TEXT, qty REAL, amount REAL, shipping REAL, discounts REAL, pay_last4 TEXT, status TEXT,
  source TEXT NOT NULL, PRIMARY KEY (order_id, asin, line)
);
CREATE TABLE IF NOT EXISTS amazon_refunds (
  order_id TEXT NOT NULL, line INTEGER NOT NULL, refund_date TEXT, amount REAL, qty REAL, reason TEXT, status TEXT,
  PRIMARY KEY (order_id, line)
);
CREATE TABLE IF NOT EXISTS amazon_matches (
  txn_id TEXT PRIMARY KEY, order_id TEXT, method TEXT NOT NULL, confidence REAL, items TEXT, discount REAL
);
DROP VIEW IF EXISTS v_crypto_transfers;
CREATE VIEW v_crypto_transfers AS
  SELECT t.*, p.usd AS price_usd, ROUND(t.amount * p.usd, 2) AS usd_value, ROUND(t.fee * p.usd, 2) AS fee_usd
  FROM crypto_transfers t LEFT JOIN prices p ON p.symbol = t.coin
   AND p.date = (SELECT MAX(date) FROM prices WHERE symbol = t.coin AND date <= substr(t.date, 1, 10));
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
  WHERE t.account_id NOT IN (SELECT account_id FROM superseded)
    AND t.txn_id NOT IN (SELECT txn_id FROM late_duplicates);
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
  SELECT account_id, account_name, institution, source, balance, as_of, snapshot_date, kind,
         COALESCE(feed_limit, manual_limit) AS credit_limit,
         CASE WHEN feed_limit IS NOT NULL THEN source WHEN manual_limit IS NOT NULL THEN 'manual' END AS credit_limit_source
  FROM (SELECT b.account_id, b.account_name, b.institution, b.source, b.bal AS balance, b.as_of, b.snapshot_date, b.kind,
               CASE WHEN CAST(b.credit_limit AS REAL) > 0 THEN CAST(b.credit_limit AS REAL) END AS feed_limit,
               (SELECT m.credit_limit FROM credit_limits m WHERE b.account_name LIKE m.pattern
                ORDER BY length(m.pattern) DESC LIMIT 1) AS manual_limit
        FROM v_balances_latest b);
"""


def upsert_sql(table: str) -> str:
    return f"""
INSERT INTO {table} (txn_id,account_id,account_name,institution,posted,transacted_at,amount,
                  description,payee,memo,pending,currency,first_seen,last_seen,source)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(txn_id) DO UPDATE SET
  account_name=excluded.account_name, institution=excluded.institution, posted=excluded.posted,
  transacted_at=excluded.transacted_at, amount=excluded.amount, description=excluded.description,
  payee=excluded.payee, memo=COALESCE(NULLIF(excluded.memo,''), {table}.memo), pending=excluded.pending, currency=excluded.currency,
  last_seen=excluded.last_seen, source=excluded.source
"""


UPSERT_BALANCE = """
INSERT OR REPLACE INTO balances (snapshot_date,source,account_id,account_name,institution,balance,available,
                                 currency,as_of)
VALUES (?,?,?,?,?,?,?,?,?)
"""

# Same as UPSERT_BALANCE plus the card's credit limit (10th value; None when the feed has none).
UPSERT_BALANCE_LIMIT = """
INSERT OR REPLACE INTO balances (snapshot_date,source,account_id,account_name,institution,balance,available,
                                 currency,as_of,credit_limit)
VALUES (?,?,?,?,?,?,?,?,?,?)
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


LATE_DUP_LAG_DAYS = 7   # a copy that first shows up this long after its own date...
LATE_DUP_WINDOW_DAYS = 4  # ...within this many days of an identical earlier charge is a feed duplicate


def flag_late_duplicates(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """Mark feed duplicates so v_ledger drops them: a posted charge that arrives LATE_DUP_LAG_DAYS+ after its
    date and repeats an identical posted charge (same account, amount and description) from 0-4 days earlier
    that the feed delivered first. Seen on the Coinbase One Card through Plaid (2026-10: the copy never moved
    the card balance). Rows from an account's first import are never flagged: legitimate repeat purchases
    (the same coffee every morning) all arrive together then. Reversible: DELETE FROM late_duplicates."""
    rows = db.execute("""
        SELECT b.txn_id, a.txn_id FROM txns a JOIN txns b
          ON a.account_id = b.account_id AND a.amount = b.amount AND a.description = b.description
         AND a.txn_id <> b.txn_id AND a.pending = 0 AND b.pending = 0
         AND b.posted BETWEEN a.posted AND date(a.posted, ?)
         AND a.first_seen < b.first_seen
        WHERE julianday(substr(b.first_seen, 1, 10)) - julianday(b.posted) >= ?
          AND substr(b.first_seen, 1, 10) > (SELECT date(MIN(first_seen), '+1 day') FROM txns c
                                            WHERE c.account_id = b.account_id)
          AND b.txn_id NOT IN (SELECT txn_id FROM late_duplicates)
          AND a.txn_id NOT IN (SELECT txn_id FROM late_duplicates)
        ORDER BY b.posted, a.posted DESC""", (f"+{LATE_DUP_WINDOW_DAYS} days", LATE_DUP_LAG_DAYS)).fetchall()
    used_orig = {r[0] for r in db.execute("SELECT dup_of FROM late_duplicates")}
    out, seen = [], set()
    for dup, orig in rows:  # one copy per original
        if dup in seen or orig in used_orig:
            continue
        seen.add(dup); used_orig.add(orig); out.append((dup, orig))
    now = datetime.now(timezone.utc).isoformat()
    db.executemany("INSERT OR IGNORE INTO late_duplicates VALUES (?,?,?,?)",
                   [(d, o, "identical charge re-delivered late by the feed", now) for d, o in out])
    db.commit()
    return out


def personal_rules() -> list[tuple]:
    """The owner's extra rules from personal.json ("rules": [[priority, pattern, kind, direction, class, note]])."""
    out = []
    for r in personal.load().get("rules") or []:
        if len(r) != 6:
            raise personal.PersonalConfigError(f"personal.json rule needs 6 fields, got {r!r}")
        out.append(tuple(r))
    return out


def personal_credit_limits() -> list[tuple[str, float]]:
    """Manual card limits from personal.json ("credit_limits": {"<account_name LIKE pattern>": limit}), for cards
    whose feed reports none. Keys starting with _ are comments."""
    out = []
    for pat, lim in (personal.load().get("credit_limits") or {}).items():
        if pat.startswith("_"):
            continue
        if isinstance(lim, bool) or not isinstance(lim, (int, float)) or lim <= 0:
            raise personal.PersonalConfigError(f"personal.json credit_limits[{pat!r}] must be a positive number")
        out.append((pat, float(lim)))
    return out


# account-name regex -> kind, first match wins (guess_kinds); a name matching none stays unclassified
KIND_GUESSES = [
    (r"\bHSA\b|HEALTH SAVINGS", "hsa"),
    (r"\bIRA\b|\bROTH\b|401\(?K|403\(?B|PENSION|RETIREMENT|ROLLOVER", "retirement"),
    (r"MORTGAGE|\bLOAN\b|\bHELOC\b|LINE OF CREDIT", "loan"),
    (r"SAVINGS?\b|MONEY MARKET|\bCD\b|CERTIFICATE", "savings"),
    (r"CHECKING|SPENDING|CASH MANAGEMENT|\bDEBIT\b", "checking"),
    (r"\bCARD\b|\bVISA\b|MASTERCARD|AMEX|AMERICAN EXPRESS|CREDIT|REWARDS|SAPPHIRE|FREEDOM|QUICKSILVER|PLATINUM"
     r"|DISCOVER", "credit"),
    (r"BROKERAGE|INVEST|INDIVIDUAL|\bSTOCKS?\b|\bJOINT\b", "investment"),
    (r"BITCOIN|\bBTC\b|CRYPTO|ETHEREUM|\bWALLET\b", "crypto"),
]


def guess_kinds(db: sqlite3.Connection) -> tuple[list[tuple[str, str]], list[str]]:
    """Give every account that has no kind one guessed from its name (note 'guessed from the name'); for ledgers
    nobody classifies by hand (personal.json "auto_kind": true). Returns ([(account name, kind)] newly set,
    [names still unclassified])."""
    import re
    done, left = [], []
    for aid, name in db.execute("SELECT account_id, account_name FROM v_accounts WHERE kind IS NULL").fetchall():
        kind = next((k for rx, k in KIND_GUESSES if re.search(rx, name or "", re.I)), None)
        if kind:
            db.execute("INSERT OR IGNORE INTO account_kind VALUES (?,?,?)", (aid, kind, "guessed from the name"))
            done.append((name, kind))
        else:
            left.append(name or aid)
    db.commit()
    return done, left


def connect(path) -> sqlite3.Connection:
    """Open the DB, creating tables and migrating pre-`source` databases in place."""
    db = sqlite3.connect(path)
    cols = {r[1] for r in db.execute("PRAGMA table_info(txns)")}
    if cols and "source" not in cols:
        db.execute("ALTER TABLE txns ADD COLUMN source TEXT NOT NULL DEFAULT 'simplefin'")
    bcols = {r[1] for r in db.execute("PRAGMA table_info(balances)")}
    if bcols and "credit_limit" not in bcols:
        db.execute("ALTER TABLE balances ADD COLUMN credit_limit TEXT")
    db.executescript(SCHEMA)
    db.execute("DELETE FROM credit_limits")  # personal.json is the source of truth
    db.executemany("INSERT INTO credit_limits VALUES (?,?)", personal_credit_limits())
    if "discount" not in {r[1] for r in db.execute("PRAGMA table_info(amazon_matches)")}:
        db.execute("ALTER TABLE amazon_matches ADD COLUMN discount REAL")
    # UNIQUE treats NULLs as distinct, so match with IS to avoid re-adding rules that have no kind/direction
    db.execute("DELETE FROM txn_rules WHERE rowid NOT IN "
               "(SELECT MIN(rowid) FROM txn_rules GROUP BY pattern, kind, direction)")
    db.executemany("INSERT INTO txn_rules (priority,pattern,kind,direction,class,note) SELECT ?,?,?,?,?,? "
                   "WHERE NOT EXISTS (SELECT 1 FROM txn_rules WHERE pattern = ? AND kind IS ? AND direction IS ?)",
                   [(*r, r[1], r[2], r[3]) for r in BASE_RULES + personal_rules()])
    db.commit()
    return db
