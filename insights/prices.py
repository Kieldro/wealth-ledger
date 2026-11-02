#!/usr/bin/env python3
"""Daily USD prices of the units the dashboard can show every value in (bitcoin, metals and two currencies), plus
two stock indexes the net-worth trend can overlay as benchmarks.

Table `prices(date, symbol, usd, source)`: one row per day per symbol, always USD per one unit:
  BTC one bitcoin; XAU, XAG, XPT one troy ounce of gold, silver, platinum; HG one pound of copper;
  IRR one Iranian rial (about 1.9 million to the dollar, so values become enormous);
  KWD one Kuwaiti dinar (the most valuable currency unit, about $3.25, so values shrink).
  SPX, IXIC  the S&P 500 and Nasdaq Composite index levels (price indexes, dividends not included). BENCHMARKS:
             trend overlays only, never a display unit.
Free, keyless sources only:
  BTC  Coinbase Exchange daily candles (UTC days, 300 per request, paged), falling back to Kraken's public OHLC
       (only its last 720 days, so it can top up but not backfill).
  XAU  Yahoo Finance COMEX gold front-month futures (GC=F) daily closes, close to spot (Stooq's CSV now sits behind
       a JavaScript bot check and LBMA's JSON returns 403), falling back to Coinbase PAX Gold (PAXG-USD, a token
       backed by one fine troy ounce; recent years only).
       Gold only trades on weekdays: weekends and holidays have no row, and lookups take the nearest earlier date.
  XAG, XPT, HG  Yahoo COMEX/NYMEX front-month futures (SI=F, PL=F, HG=F; copper is quoted in USD per pound).
  IRR, KWD      Yahoo exchange rates (IRR=X, KWD=X are units per dollar; stored inverted as USD per unit). Only
                the latest rate is kept (LATEST_ONLY): the dashboard converts every date at today's rate, since
                Yahoo's rial history is the official 42,000 until Jan 2026 and the market rate after it.
  SPX, IXIC     Yahoo index closes (^GSPC, ^IXIC); weekdays only, like gold.

The first run backfills from a few days before the earliest date the trend needs (the first networth_history point
or the first daily balance snapshot). Later runs only re-fetch the last few days (today's candle is still moving),
which is one request per symbol.

  python -m insights.prices             fetch and store
  python -m insights.prices --dry-run   show what would be fetched and stored, write nothing
Never sends notifications (owner's rule). Errors print one `error: ...` line and exit 1.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctdb  # noqa: E402

DEFAULT_DB = Path(os.environ.get("WEALTH_LEDGER_DB") or "~/.local/share/wealth-ledger/txns.db").expanduser()
SYMBOLS = ("BTC", "XAU", "XAG", "XPT", "HG", "IRR", "KWD")  # display units
BENCHMARKS = ("SPX", "IXIC")  # trend overlays only (BTC and XAU double as overlays too)
UA = {"User-Agent": "Mozilla/5.0 (wealth-ledger prices)"}
LEAD_DAYS = 7       # fetch this many days before the earliest needed date, so a weekend/holiday start has a price
REFETCH_DAYS = 3    # re-fetch the last few stored days each run: the latest candle is still in progress
PAGE = 300          # Coinbase returns at most 300 candles per request
PAUSE = 0.5         # seconds between paged requests
PLAUSIBLE = {"BTC": (100, 10_000_000), "XAU": (200, 50_000), "XAG": (3, 2_000), "XPT": (100, 50_000),
             "HG": (0.5, 100), "IRR": (1e-8, 5e-6), "KWD": (1, 20),
             "SPX": (100, 1_000_000), "IXIC": (100, 1_000_000)}  # USD per unit; anything outside is a bad row
LATEST_ONLY = {"IRR", "KWD"}  # no backfill: these convert at today's rate everywhere
SIG_DIGITS = 8  # stored precision: significant digits, since a rial is a fraction of a millionth of a dollar


def _get(url: str, timeout: int = 30):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as f:
        return json.load(f)


def _ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _utc_date(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).date().isoformat()


# ---------------------------------------------------------------- sources: each -> {date: usd}

def coinbase_candles(product: str, start: date, end: date, sleep=time.sleep) -> dict[str, float]:
    """Daily closes of a Coinbase Exchange product for UTC days start..end, paging 300 candles per request.
    Rows are [time, low, high, open, close, volume], newest first."""
    out: dict[str, float] = {}
    cur = start
    first = True
    while cur <= end:
        stop = min(end, cur + timedelta(days=PAGE - 1))
        if not first:
            sleep(PAUSE)
        first = False
        qs = urllib.parse.urlencode({"granularity": 86400, "start": f"{cur.isoformat()}T00:00:00Z",
                                     "end": f"{stop.isoformat()}T00:00:00Z"})
        rows = _get(f"https://api.exchange.coinbase.com/products/{product}/candles?{qs}")
        if not isinstance(rows, list):
            raise RuntimeError(f"coinbase {product}: unexpected reply {str(rows)[:120]}")
        for r in rows:
            d = _utc_date(r[0])
            if start.isoformat() <= d <= end.isoformat():
                out[d] = float(r[4])
        cur = stop + timedelta(days=1)
    return out


def kraken_ohlc(start: date, end: date) -> dict[str, float]:
    """BTC daily closes from Kraken (only its most recent 720 days). Rows: [time, open, high, low, close, ...]."""
    d = _get(f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440&since={_ts(start) - 1}")
    if d.get("error"):
        raise RuntimeError(f"kraken: {'; '.join(d['error'])}")
    rows = next(v for k, v in d["result"].items() if k != "last")
    return {day: float(r[4]) for r in rows if start.isoformat() <= (day := _utc_date(r[0])) <= end.isoformat()}


def yahoo_daily(symbol: str, start: date, end: date) -> dict[str, float]:
    """Daily closes from Yahoo's chart API, dated in the exchange's time zone (null closes skipped)."""
    qs = urllib.parse.urlencode({"interval": "1d", "period1": _ts(start), "period2": _ts(end + timedelta(days=1))})
    d = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{qs}")
    res = (d.get("chart") or {}).get("result")
    if not res:
        raise RuntimeError(f"yahoo {symbol}: {(d.get('chart') or {}).get('error') or 'no result'}")
    res = res[0]
    meta = res.get("meta") or {}
    try:  # bars sit at local midnight; gmtoffset is only today's offset (wrong across daylight saving)
        tz = ZoneInfo(meta["exchangeTimezoneName"])
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        tz = timezone(timedelta(seconds=int(meta.get("gmtoffset") or 0)))
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out = {}
    for t, c in zip(res.get("timestamp") or [], closes):
        day = datetime.fromtimestamp(t, tz).date().isoformat()
        if c is not None and start.isoformat() <= day <= end.isoformat():
            out[day] = float(c)
    return out


def per_unit(px: dict[str, float]) -> dict[str, float]:
    """Exchange rates quoted as units per dollar -> USD per unit."""
    return {d: 1 / v for d, v in px.items() if v > 0}


def _weekdays(px: dict[str, float]) -> dict[str, float]:
    return {d: v for d, v in px.items() if date.fromisoformat(d).weekday() < 5}


SOURCES = {  # symbol -> [(source name, fetch(start, end) -> {date: usd})], best first
    "BTC": [("coinbase", lambda s, e: coinbase_candles("BTC-USD", s, e)),
            ("kraken", kraken_ohlc)],
    "XAU": [("yahoo GC=F", lambda s, e: yahoo_daily("GC=F", s, e)),
            ("coinbase PAXG", lambda s, e: _weekdays(coinbase_candles("PAXG-USD", s, e)))],
    "XAG": [("yahoo SI=F", lambda s, e: yahoo_daily("SI=F", s, e))],
    "XPT": [("yahoo PL=F", lambda s, e: yahoo_daily("PL=F", s, e))],
    "HG": [("yahoo HG=F", lambda s, e: yahoo_daily("HG=F", s, e))],
    "IRR": [("yahoo IRR=X", lambda s, e: per_unit(yahoo_daily("IRR=X", s, e)))],
    "KWD": [("yahoo KWD=X", lambda s, e: per_unit(yahoo_daily("KWD=X", s, e)))],
    "SPX": [("yahoo ^GSPC", lambda s, e: yahoo_daily("^GSPC", s, e))],
    "IXIC": [("yahoo ^IXIC", lambda s, e: yahoo_daily("^IXIC", s, e))],
}


def fetch(symbol: str, start: date, end: date) -> tuple[dict[str, float], str]:
    """-> ({date: usd}, source name) from the first source that returns plausible rows."""
    errors = []
    for name, fn in SOURCES[symbol]:
        try:
            px = fn(start, end)
        except Exception as e:  # noqa: BLE001 - try the next source
            errors.append(f"{name}: {e}")
            continue
        lo, hi = PLAUSIBLE[symbol]
        px = {d: v for d, v in px.items() if lo <= v <= hi}
        if px:
            return px, name
        errors.append(f"{name}: no rows for {start}..{end}")
    raise RuntimeError(f"{symbol}: " + "; ".join(errors))


# ---------------------------------------------------------------- storage

def needed_start(db: sqlite3.Connection) -> date | None:
    """Earliest date the trend shows: the first networth_history point or the first daily balance snapshot."""
    ds = []
    for sql in ("SELECT MIN(date) FROM networth_history", "SELECT MIN(snapshot_date) FROM balances"):
        try:
            v = db.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            v = None
        if v:
            ds.append(date.fromisoformat(v[:10]))
    return min(ds) if ds else None


def plan(db: sqlite3.Connection, symbol: str, today: date) -> list[tuple[date, date]]:
    """Date ranges to fetch: a backfill if stored history doesn't reach the earliest needed date, plus the recent tail."""
    earliest = today if symbol in LATEST_ONLY else (needed_start(db) or today)
    lo, hi = db.execute("SELECT MIN(date), MAX(date) FROM prices WHERE symbol = ?", (symbol,)).fetchone()
    if lo is None:
        return [(earliest - timedelta(days=LEAD_DAYS), today)]
    ranges = []
    if date.fromisoformat(lo) > earliest:
        ranges.append((earliest - timedelta(days=LEAD_DAYS), date.fromisoformat(lo)))
    ranges.append((min(date.fromisoformat(hi), today) - timedelta(days=REFETCH_DAYS), today))
    return ranges


def update(db: sqlite3.Connection, today: date | None = None, dry_run: bool = False) -> dict[str, dict]:
    """Fetch what's missing for every symbol and store it. -> {symbol: {"rows": n, "source": ..., "ranges": [...]}}.
    Raises RuntimeError naming every symbol that failed (after storing the ones that worked)."""
    today = today or datetime.now(timezone.utc).date()
    report, errors = {}, []
    for sym in SOURCES:
        ranges = plan(db, sym, today)
        got: dict[str, float] = {}
        srcs = set()
        try:
            for s, e in ranges:
                px, src = fetch(sym, s, e)
                got.update(px)
                srcs.add(src)
        except RuntimeError as e:
            errors.append(str(e))
            continue
        report[sym] = {"rows": len(got), "source": ", ".join(sorted(srcs)),
                       "ranges": [(s.isoformat(), e.isoformat()) for s, e in ranges]}
        if not dry_run and got:
            db.executemany("INSERT OR REPLACE INTO prices (date, symbol, usd, source) VALUES (?,?,?,?)",
                           [(d, sym, float(f"{v:.{SIG_DIGITS}g}"), src) for d, v in sorted(got.items())])
            db.commit()
    if errors:
        raise RuntimeError("; ".join(errors))
    return report


# ---------------------------------------------------------------- lookups (dashboard)

def asof(series: list[tuple[str, float]], dates) -> dict[str, float]:
    """series: [(date, usd)] ascending. -> {date: usd of that date or the nearest earlier one}; dates before the
    first price are left out."""
    keys = [d for d, _ in series]
    out = {}
    for d in dates:
        i = bisect.bisect_right(keys, d) - 1
        if i >= 0:
            out[d] = series[i][1]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        db = ctdb.connect(args.db)
        report = update(db, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001 - one line for the sync log
        print(f"error: prices: {e}", file=sys.stderr)
        return 1
    parts = []
    for sym, r in report.items():
        lo, hi, n = db.execute("SELECT MIN(date), MAX(date), COUNT(*) FROM prices WHERE symbol = ?", (sym,)).fetchone()
        fetched = ", ".join(f"{s}..{e}" for s, e in r["ranges"])
        parts.append(f"{sym} {r['rows']} fetched ({fetched}) from {r['source']}; stored {n} rows {lo}..{hi}")
    print(("dry run: " if args.dry_run else "prices: ") + "; ".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
