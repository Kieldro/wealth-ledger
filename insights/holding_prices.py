#!/usr/bin/env python3
"""Daily closes of the securities held in counted accounts, so the dashboard trend can value each day's holdings at
that day's market close instead of at the feed's own balance.

Why: SimpleFIN delivers brokerage balances and holding prices a day or two late and irregularly (a balance can sit
unchanged for days, then jump), so a trend built from those balances shows market moves late and its correlation
with a benchmark is paired with the wrong days.

Table `security_prices(date, symbol, usd, source)`: one row per trading day per symbol, the symbol spelled as the
feed spells it (BF.B). Kept apart from `prices` so a ticker can never be mistaken for a display unit (BTC is also a
fund's ticker). Source: Yahoo Finance's keyless chart API, the one insights/prices.py already uses for the S&P 500 and
metals (stocks, ETFs, ADRs, OTC shares, and mutual funds at their NAV). Each close is fetched once: the first run
backfills from a few days before the symbol's first holdings snapshot, later runs re-fetch only the last few stored
days (today's bar moves until the close): one request per symbol, and none for a symbol no account holds any more.
A symbol Yahoo doesn't know stores nothing; the trend then keeps the feed's own price for it and names it.

Money-market / core cash positions (SPAXX, FDRXX, ...) are $1 a share and never fetched.

Fetched by insights/prices.py's main (the daily sync); the dashboard only reads the table (revalue()).
"""
from __future__ import annotations

import bisect
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

from insights import prices as price_hist

# accounts networth.py counts as live lines (mirrors its EXTRA_KINDS; crypto/custodial are excluded there)
COUNTED_KINDS = ("credit", "retirement", "checking", "savings", "investment", "hsa", "loan")
CASH_SYMBOLS = {"SPAXX", "SPRXX", "FDRXX", "FZFXX", "FCASH", "CORE", "USD"}
CASH_PATTERN = re.compile(r"[A-Z]{3}XX")  # US money-market fund tickers: SPAXX, VMFXX, SWVXX...
LEAD_DAYS = 7         # fetch this many days before a symbol's first snapshot, so a weekend start has a close
REFETCH_DAYS = 3      # re-fetch the last few stored days each run: today's bar is still moving
PAUSE = 0.3           # seconds between symbols (one request each once backfilled)
MAX_CLOSE_AGE = 7     # days: an older close (delisted, halted) is not used for a date; the feed's price is
SANE = (0.5, 2.0)     # a close this far from the feed's own price is a different security or a split: not used


def is_cash(symbol: str) -> bool:
    return symbol in CASH_SYMBOLS or bool(CASH_PATTERN.fullmatch(symbol))


def yahoo_symbol(symbol: str) -> str:
    """Fidelity's share-class dot (BF.B) is a dash on Yahoo (BF-B)."""
    return symbol.replace(".", "-").replace("/", "-")


def held(db: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """{symbol: (first, last snapshot date)} of every priced security in a counted, non-superseded account."""
    try:
        rows = db.execute(f"""SELECT TRIM(h.symbol), MIN(h.snapshot_date), MAX(h.snapshot_date) FROM holdings h
                              JOIN account_kind k USING (account_id)
                              WHERE k.kind IN ({','.join('?' * len(COUNTED_KINDS))})
                                AND h.account_id NOT IN (SELECT account_id FROM superseded)
                                AND TRIM(COALESCE(h.symbol, '')) <> ''
                              GROUP BY TRIM(h.symbol)""", COUNTED_KINDS).fetchall()
    except sqlite3.OperationalError:  # DB predates these tables
        return {}
    return {s: (first[:10], last[:10]) for s, first, last in rows if not is_cash(s)}


def plan(db: sqlite3.Connection, symbol: str, first: date, last: date, today: date) -> tuple[date, date] | None:
    """The one date range to fetch: from a few days before `first` while the stored closes don't reach back that far
    (nothing stored yet, or the symbol only started trading later), else the recent tail; None once the symbol has
    left every account (last snapshot over MAX_CLOSE_AGE days ago) and its closes reach that far."""
    lo, hi = db.execute("SELECT MIN(date), MAX(date) FROM security_prices WHERE symbol = ?", (symbol,)).fetchone()
    if lo is None or date.fromisoformat(lo) > first:
        return first - timedelta(days=LEAD_DAYS), today
    if (today - last).days > MAX_CLOSE_AGE and date.fromisoformat(hi) >= last:
        return None
    return min(date.fromisoformat(hi), today) - timedelta(days=REFETCH_DAYS), today


def update(db: sqlite3.Connection, today: date | None = None, dry_run: bool = False, fetch=None,
           sleep=time.sleep) -> dict:
    """Fetch and store the missing closes of every held symbol. -> {"symbols" (fetched), "rows", "failed": [symbol,
    ...]}. A symbol that fails is only reported (the trend keeps the feed's price for it); raises RuntimeError when
    every symbol failed, which means the source itself is down."""
    today = today or datetime.now(timezone.utc).date()
    fetch = fetch or (lambda sym, s, e: price_hist.yahoo_daily(yahoo_symbol(sym), s, e))
    todo = {sym: r for sym, (first, last) in held(db).items()
            if (r := plan(db, sym, date.fromisoformat(first), date.fromisoformat(last), today))}
    rows, failed, errors = 0, [], []
    for i, (sym, (start, end)) in enumerate(sorted(todo.items())):
        if i:
            sleep(PAUSE)
        try:
            got = fetch(sym, start, end)
        except Exception as e:  # noqa: BLE001 - unknown or delisted symbol, or a network error
            failed.append(sym)
            errors.append(f"{sym}: {e}")
            continue
        got = {d: v for d, v in got.items() if v > 0}
        rows += len(got)
        if not dry_run and got:
            db.executemany("INSERT OR REPLACE INTO security_prices (date, symbol, usd, source) VALUES (?,?,?,?)",
                           [(d, sym, float(f"{v:.{price_hist.SIG_DIGITS}g}"), f"yahoo {yahoo_symbol(sym)}")
                            for d, v in sorted(got.items())])
            db.commit()
    if todo and len(failed) == len(todo):
        raise RuntimeError("no close fetched for any held symbol (" + "; ".join(errors[:3]) + ")")
    return {"symbols": len(todo), "rows": rows, "failed": failed}


# ---------------------------------------------------------------- valuing (dashboard)

def load(db: sqlite3.Connection, symbols) -> dict[str, list[tuple[str, float]]]:
    """{symbol: [(date, usd), ...] ascending} of the stored closes; {} on a DB without the table."""
    symbols = sorted(set(symbols))
    if not symbols:
        return {}
    out: dict[str, list] = {}
    try:
        for sym, d, usd in db.execute(f"SELECT symbol, date, usd FROM security_prices WHERE symbol IN "
                                      f"({','.join('?' * len(symbols))}) ORDER BY symbol, date", symbols):
            out.setdefault(sym, []).append((d, usd))
    except sqlite3.OperationalError:
        return {}
    return out


def close_on(series: list[tuple[str, float]] | None, day: str) -> tuple[str, float] | None:
    """(date, usd) of the last close on or before `day` (a weekend or holiday carries the trading day before),
    or None when there is none within MAX_CLOSE_AGE days."""
    if not series:
        return None
    i = bisect.bisect_right(series, (day, float("inf"))) - 1
    if i < 0 or (date.fromisoformat(day) - date.fromisoformat(series[i][0])).days > MAX_CLOSE_AGE:
        return None
    return series[i]


def revalue(balance: float, positions, closes: dict, day: str) -> tuple[float, str | None, set[str]]:
    """One account on `day` from one holdings snapshot: shares x that day's close for every priced symbol, $1 a share
    for money-market cash, plus the snapshot's remainder (balance - sum of market values: cash and anything
    unpriced). positions: [(symbol, shares, price, market_value)] from that snapshot; closes: load().
    -> (value, newest close date used or None, symbols that kept the feed's price for want of a usable close).
    A blank-symbol position (a cash sweep, an unlisted holding) keeps its market value and is not reported."""
    value = balance - sum(mv or 0.0 for _, _, _, mv in positions)
    newest, fallback = None, set()
    for sym, shares, price, mv in positions:
        sym = (sym or "").strip()
        if not sym or shares is None:
            value += mv or 0.0
            continue
        if is_cash(sym):
            value += shares
            continue
        c = close_on(closes.get(sym), day)
        if c is None or not price or not SANE[0] <= c[1] / price <= SANE[1]:
            value += mv or 0.0
            fallback.add(sym)
            continue
        value += shares * c[1]
        newest = max(newest or c[0], c[0])
    return value, newest, fallback
