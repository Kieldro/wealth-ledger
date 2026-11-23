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
account_kind  account type (credit/checking/savings/investment/retirement/hsa/loan/crypto/custodial): set from
              personal.json "account_kinds", guessed from the account name at each sync (personal.auto_kind()), or
              put in by hand; an account with none has kind NULL in v_accounts and is left out of the totals.
txn_rules     description LIKE-patterns -> class (first match by priority wins; add rows to teach it).
              Seeded from BASE_RULES below plus the owner's own "rules" in personal.json.
txn_class_overrides  one transaction's class set by hand (txn_id -> class); beats txn_rules and the fallback.
              E.g. a fraud charge the issuer reversed after a dispute whose credit never came through the feed:
              class 'disputed'. Add rows by hand (sqlite3); nothing writes them.
valuations    daily/monthly estimates for things no bank feed covers (metals, crypto wallets, home, cars),
              keyed by the balance-sheet row label they replace in networth.py (see valuations/).
networth_history  past whole-net-worth points (Total Equity per dated balance-sheet column of the yearly
              spreadsheets), from insights/nw_history.py; the dashboard trend shows them before the daily series.
prices        daily USD closes of BTC (per coin) and XAU (per troy oz of gold), from insights/prices.py; the
              dashboard trend uses them to price net worth in bitcoin and in gold.
security_prices  daily USD closes of the stocks, ETFs and funds held in counted accounts (symbol as the feed spells
              it), from insights/holding_prices.py; the dashboard trend values each day's holdings at them.
amazon_items / amazon_refunds  items and refunds from an Amazon "Your Orders" data export (insights/amazon.py;
              no addresses, gift details or tracking). amazon_matches links an Amazon card charge (txn_id) to the
              order items it paid for (items = JSON [[asin, line], ...]; discount = rewards points that paid the
              rest, for method 'points'); insights/budget.py splits it by item.
superseded    accounts whose feed was replaced by another source (e.g. a SimpleFIN account later fed by Plaid);
              hidden from every view and no longer written by that feed. History rows are kept.
Views: v_ledger (txns + real date/amount + kind + class), v_balances_latest, v_holdings_latest (each account's
newest positions with their snapshot_date and lag_days behind the feed's newest snapshot),
v_accounts (latest balance per account + credit_limit, from the feed or else personal.json, and credit_limit_source).

class: spend | income | refund | transfer (between the owner's own accounts) | card_payment | investment |
disputed (a charge reversed by a won dispute, e.g. fraud; mark its reversal too if the feed has one).
Spending = class 'spend'; card payments and transfers show up on both sides, so they're never spend; disputed rows
count nowhere (not spending, not income, not cash flow).
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
CREATE TABLE IF NOT EXISTS txn_class_overrides (txn_id TEXT PRIMARY KEY, class TEXT NOT NULL, note TEXT,
  created TEXT DEFAULT (datetime('now')));
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
CREATE TABLE IF NOT EXISTS security_prices (
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
  -- date: posted, else when it was made, else (a pending row whose feed gave neither) the day the sync first saw it,
  -- so a date filter never drops a pending charge
  SELECT t.*, COALESCE(t.posted, date(t.transacted_at), date(t.first_seen, 'localtime')) AS date,
    CAST(t.amount AS REAL) AS amt, k.kind,
    COALESCE(
      (SELECT o.class FROM txn_class_overrides o WHERE o.txn_id = t.txn_id),
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
CREATE INDEX IF NOT EXISTS idx_holdings_account ON holdings(source, account_id, snapshot_date);
DROP VIEW IF EXISTS v_holdings_latest;
-- Each account's newest positions (not the feed's newest day: an account the feed skipped that day, e.g. its
-- connection failed, must not vanish). lag_days = how far that snapshot is behind the feed's newest one. An account
-- that is behind AND whose latest balance is zero was emptied (rolled over, closed): its old positions are left out.
CREATE VIEW v_holdings_latest AS
  SELECT h.*, CAST(h.market_value AS REAL) AS value,
         CAST(julianday(s.newest) - julianday(h.snapshot_date) AS INTEGER) AS lag_days
  FROM holdings h
  JOIN (SELECT source, MAX(snapshot_date) AS newest FROM holdings GROUP BY source) s ON s.source = h.source
  WHERE h.snapshot_date = (SELECT MAX(h2.snapshot_date) FROM holdings h2
                           WHERE h2.source = h.source AND h2.account_id = h.account_id)
    AND h.account_id NOT IN (SELECT account_id FROM superseded)
    AND (h.snapshot_date = s.newest
         OR NOT EXISTS (SELECT 1 FROM v_balances_latest b
                        WHERE b.account_id = h.account_id AND ABS(COALESCE(b.bal, 0)) < 0.01));
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
    # the checking side of a card payment, as banks commonly word it (the purchases on the card are the spending);
    # a debit-card purchase is spending whatever else its text says ("DEBIT CARD PAYMENT TO ...")
    (14, "%DEBIT CARD%", "checking", "out", "spend", "debit-card purchase"),
    (15, "%CREDIT CARD%", "checking", "out", "card_payment", "card payment from checking"),
    (15, "%CREDIT CRD%", "checking", "out", "card_payment", "card payment from checking"),
    (15, "%CRCARDPMT%", "checking", "out", "card_payment", "card payment from checking"),
    (15, "%CARD PAYMENT%", "checking", "out", "card_payment", "card payment from checking"),
    (15, "%CARD PMT%", "checking", "out", "card_payment", "card payment from checking"),
    (15, "%CARD ONLINE PAYMENT%", "checking", "out", "card_payment", "card payment from checking"),
    (15, "%EPAYMENT%", "checking", "out", "card_payment", "card payment from checking"),
    (15, "%E-PAYMENT%", "checking", "out", "card_payment", "card payment from checking"),
    # between the owner's own checking and savings
    (20, "%TRANSFER TO SAV%", None, None, "transfer", "own checking <-> savings"),
    (20, "%TRANSFER FROM SAV%", None, None, "transfer", "own checking <-> savings"),
    (20, "%TRANSFER TO CHK%", None, None, "transfer", "own checking <-> savings"),
    (20, "%TRANSFER FROM CHK%", None, None, "transfer", "own checking <-> savings"),
    (20, "%TRANSFER TO CHECKING%", None, None, "transfer", "own checking <-> savings"),
    (20, "%TRANSFER FROM CHECKING%", None, None, "transfer", "own checking <-> savings"),
    # moving money between the owner's own accounts (brokerage feed formats)
    (20, "%CORE ACCOUNT%", None, None, "transfer", "Fidelity cash sweep"),
    (20, "REINVESTMENT%", None, None, "transfer", "money-market reinvest"),
    (20, "%Electronic Funds Transfer%", None, None, "transfer", "Fidelity EFT to own bank"),
    (20, "%TRANSFERRED FROM%", None, None, "transfer", "Fidelity internal transfer"),
    (20, "%MONEYLINE%", None, None, "transfer", "Fidelity MoneyLine pull from own bank"),
    (20, "%TRANSFERRED TO VS%", None, None, "transfer", "Fidelity internal transfer, sending side"),
    (20, "%TRANSFER OF ASSETS%", None, None, "transfer", "Fidelity: account moved in from another custodian"),
    (20, "%Fidelity Crypto a/c%", None, None, "transfer", "Fidelity brokerage <-> Fidelity Crypto"),
    (20, "NORMAL DISTR%VS %", "hsa", "out", "transfer", "Fidelity HSA distribution paid to own Fidelity account"),
    (20, "%ACCTVERIFY%", None, None, "transfer", "bank-link verification micro-deposits"),
    # buying assets is not spending
    (30, "%YOU BOUGHT%", None, "out", "investment", "security bought in a cash or HSA account"),
    (30, "%YOU SOLD%", None, "in", "investment", "sale proceeds"),
    (30, "DEBIT CARD %", None, "in", "refund", "Fidelity debit card return or adjustment"),
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
    that the feed delivered first. Seen on a credit card through Plaid (the copy never moved
    the card balance), so only Plaid rows are ever flagged; no other feed has shown it and a late import of real
    history (fidelity_web) must not lose repeat purchases. Rows from an account's first import are never flagged: legitimate repeat purchases
    (the same coffee every morning) all arrive together then. Reversible: DELETE FROM late_duplicates."""
    rows = db.execute("""
        SELECT b.txn_id, a.txn_id FROM txns a JOIN txns b
          ON a.account_id = b.account_id AND a.amount = b.amount AND a.description = b.description
         AND a.txn_id <> b.txn_id AND a.pending = 0 AND b.pending = 0
         AND a.source = 'plaid' AND b.source = 'plaid'
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


HOLDINGS_LAG_DAYS = 3  # positions this many days behind the feed's newest snapshot are named by the sync


def lagging_holdings(db: sqlite3.Connection, source: str, min_days: int = HOLDINGS_LAG_DAYS) -> list[tuple]:
    """[(account name, snapshot_date, lag_days)] for accounts of `source` whose positions in v_holdings_latest are
    min_days+ older than the feed's newest snapshot: the account still has a balance but the feed stopped sending
    its positions, so holdings answers show it as of that older day."""
    return db.execute("SELECT account_name, snapshot_date, MAX(lag_days) FROM v_holdings_latest "
                      "WHERE source = ? AND lag_days >= ? GROUP BY account_id ORDER BY 2, 1",
                      (source, min_days)).fetchall()


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


KINDS = ("checking", "savings", "credit", "investment", "retirement", "hsa", "loan", "crypto", "custodial")
FROM_CONFIG = "personal.json account_kinds"
GUESSED = "guessed from the name"


def personal_account_kinds() -> list[tuple[str, str]]:
    """Account types set in personal.json ("account_kinds": {"<account_name LIKE pattern>": "<kind>"}).
    Keys starting with _ are comments."""
    out = []
    for pat, kind in (personal.load().get("account_kinds") or {}).items():
        if pat.startswith("_"):
            continue
        if kind not in KINDS:
            raise personal.PersonalConfigError(
                f"personal.json account_kinds[{pat!r}] must be one of: {', '.join(KINDS)}")
        out.append((pat, kind))
    return out


def apply_account_kinds(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """Give accounts the type personal.json "account_kinds" names for them (longest matching pattern wins). It
    fills in an account with no type and replaces a guessed or earlier configured one; a type someone put into
    the account_kind table by hand is left alone. Returns [(account name, kind)] newly set or changed."""
    pats = sorted(personal_account_kinds(), key=lambda pk: -len(pk[0]))
    done = []
    if not pats:
        return done
    rows = db.execute("SELECT a.account_id, a.account_name, k.kind, k.note FROM v_accounts a "
                      "LEFT JOIN account_kind k USING (account_id)").fetchall()
    for aid, name, kind, note in rows:
        want = next((k for p, k in pats if db.execute("SELECT ? LIKE ?", (name or "", p)).fetchone()[0]), None)
        if not want or want == kind or (kind and note not in (GUESSED, FROM_CONFIG, "holds positions")):
            continue
        db.execute("INSERT OR REPLACE INTO account_kind VALUES (?,?,?)", (aid, want, FROM_CONFIG))
        done.append((name, want))
    db.commit()
    return done


def guess_kinds(db: sqlite3.Connection) -> tuple[list[tuple[str, str]], list[str]]:
    """Give every account that has no kind one guessed from its name (note 'guessed from the name'), or
    'investment' when the name says nothing but the feed lists positions in it; for ledgers nobody classifies by
    hand (personal.auto_kind()). Returns ([(account name, kind)] newly set, [names still unclassified])."""
    import re
    done, left = [], []
    for aid, name in db.execute("SELECT account_id, account_name FROM v_accounts WHERE kind IS NULL").fetchall():
        kind, note = next((k for rx, k in KIND_GUESSES if re.search(rx, name or "", re.I)), None), GUESSED
        if not kind and db.execute("SELECT 1 FROM v_holdings_latest WHERE account_id = ? AND value > 0 LIMIT 1",
                                   (aid,)).fetchone():
            kind, note = "investment", "holds positions"
        if kind:
            db.execute("INSERT OR IGNORE INTO account_kind VALUES (?,?,?)", (aid, kind, note))
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
