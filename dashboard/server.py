#!/usr/bin/env python3
"""Private finance dashboard for wealth-ledger. Read-only; serves on 127.0.0.1 + the Tailscale IP only.

  server.py [--port 8766]
  server.py set-password [--generate]     # set/rotate the login password (signs out all sessions)

Every path needs a login session (cookie) except /login, /logout (POST) and /robots.txt.
Endpoints: /api/summary, POST /api/refresh (re-runs networth.py), /api/trend, /api/accounts,
/api/holdings, /api/crypto, /api/cards, /api/spending, /api/drafts (overdraft forecast), /api/budget (budget vs actual),
/api/subscriptions (recurring charges + unusual charges), /api/rental (rental P&L),
/api/inherited_ira (10-year rule tracker), /api/last (the last good answer to each of those, kept on disk), /healthz. Page: /
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import string
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape as html_escape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import personal  # noqa: E402
from insights import budget, inherited_ira, monthly_email, overdraft, rental, subscriptions  # noqa: E402
from insights import prices as price_hist  # noqa: E402

_DASH = personal.section("dashboard")
DB = os.path.expanduser(os.environ.get("WEALTH_LEDGER_DB") or "~/.local/share/wealth-ledger/txns.db")
STATUS = os.path.expanduser(os.environ.get("WEALTH_LEDGER_STATUS") or "~/.cache/wealth-ledger-status")
NETWORTH = [os.path.join(ROOT, ".venv", "bin", "python"), os.path.join(ROOT, "networth.py"), "--json"]
CACHE_TTL = 600
STATUS_MAX_AGE_H = 36  # the sync runs daily; a last run older than this means it hung, died or isn't scheduled
LEDGER_START = _DASH.get("ledger_start") or "1970-01-01"  # first day of complete transaction data
# accounts networth.py counts as live lines (mirrors its EXTRA_KINDS; crypto/custodial are excluded there)
NW_KINDS = ("credit", "retirement", "checking", "savings", "investment", "hsa", "loan")

# ---------- categories ----------
CATEGORIES = [  # key, label, type
    ("cash", "Cash & banking", "asset"),
    ("retirement", "Retirement", "asset"),
    ("taxable", "Taxable brokerage", "asset"),
    ("crypto", "Crypto", "asset"),
    ("metals", "Precious metals", "asset"),
    ("realestate", "Real estate", "asset"),
    ("vehicles", "Vehicles", "asset"),
    ("other", "Other", "asset"),
    ("mortgage", "Mortgage", "liability"),
    ("cards", "Credit cards & other debt", "liability"),
]
# networth.py line label -> category, then (regex on line label, category) fallbacks; both from personal.json
# "dashboard" because the labels are the owner's own balance-sheet rows. Lines matching neither go to "other".
DEFAULT_RULES = [
    (r"\b(ira|roth|401\(?k|403b|hsa|pre-tax|pension)\b", "retirement"),
    (r"checking|savings|bank|cash|wallet", "cash"),
    (r"crypto|bitcoin", "crypto"),
    (r"metal|gold|silver", "metals"),
    (r"home|house|property", "realestate"),
    (r"car\b|truck|vehicle", "vehicles"),
    (r"brokerage|individual|taxable", "taxable"),
]
CRYPTO_LINE = "Total cryptocurrency (USD)"  # networth.py line and valuations asset holding every coin
CRYPTO_CASH_ROWS = set(personal.section("crypto").get("exchange_rows") or [])  # wallets with their own cash row
READ_QTY = {"onchain", "exchange-api", "simplefin"}  # amounts read from a chain or exchange (mirrors networth.py)
EXACT: dict[str, str] = dict(_DASH.get("exact") or {})
RULES: list[tuple[str, str]] = [tuple(r) for r in _DASH["rules"]] if _DASH.get("rules") else DEFAULT_RULES


def categorize(line: dict) -> str:
    name = line["line"]
    if line["type"] == "liability":
        return "mortgage" if re.search(r"mortgage", name, re.I) else "cards"
    if name in EXACT:
        return EXACT[name]
    for pat, cat in RULES:
        if re.search(pat, name, re.I):
            return cat
    return "other"


# ---------- data access ----------
def db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def q(sql: str, args=()) -> list[dict]:
    con = db()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


_nw_lock = threading.Lock()
_nw = {"data": None, "fetched": 0.0, "error": None}


def networth(refresh: bool = False) -> dict:
    with _nw_lock:
        fresh = _nw["data"] is not None and time.time() - _nw["fetched"] < CACHE_TTL
        if refresh or not fresh:
            try:
                out = subprocess.run(NETWORTH, capture_output=True, text=True, timeout=60, check=True).stdout
                _nw.update(data=json.loads(out), fetched=time.time(), error=None)
            except Exception as e:  # keep serving the stale copy
                msg = getattr(e, "stderr", "") or str(e)
                _nw["error"] = f"networth.py failed: {str(msg).strip()[-300:]}"
        return dict(_nw)


def read_status(now: datetime | None = None) -> dict:
    """The sync's status file, plus a problem of our own when no sync has finished lately: the file is only
    rewritten at the end of a run, so a sync that hangs, is killed or stops being scheduled leaves an old
    "rc=0" behind that would otherwise read as healthy."""
    try:
        lines = [l.rstrip() for l in open(STATUS) if l.strip()]
    except OSError as e:
        c = connect_state()
        if c and (c["needed"] or c["running"]):  # nothing has synced yet: the page shows how to connect instead
            return {"last_run": None, "rc": None, "problems": []}
        return {"last_run": None, "rc": None, "problems": [f"status file unreadable: {e.strerror}"]}
    m = re.match(r"last run (\S+) rc=(\d+)", lines[0]) if lines else None
    out = {"last_run": m.group(1) if m else None, "rc": int(m.group(2)) if m else None,
           "problems": lines[1:] if m else lines}
    try:
        age = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(out["last_run"]).astimezone(timezone.utc)
    except (TypeError, ValueError):  # no first line, or not a time
        out["problems"].insert(0, "The status file doesn't say when the sync last finished")
        return out
    if age > timedelta(hours=STATUS_MAX_AGE_H):
        out["problems"].insert(0, f"No sync has finished since {out['last_run'][:10]} ({age.days} "
                                  f"day{'s' if age.days != 1 else ''} ago): the numbers here may be that old")
    return out


def synced_series() -> list[dict]:
    """Daily sum of synced balances networth.py counts (non-custodial/crypto, non-superseded), carried forward.
    Each day also has "accounts" (account_id -> balance that day) so period_changes can compare like with like."""
    rows = q(f"""SELECT b.snapshot_date d, b.account_id a, CAST(b.balance AS REAL) bal FROM balances b
                 JOIN account_kind k USING (account_id)
                 WHERE k.kind IN ({','.join('?' * len(NW_KINDS))})
                   AND b.account_id NOT IN (SELECT account_id FROM superseded)
                 ORDER BY b.snapshot_date""", NW_KINDS)
    dates = sorted({r["d"] for r in rows})
    last, out = {}, []
    by_date: dict[str, list] = {}
    for r in rows:
        by_date.setdefault(r["d"], []).append(r)
    for d in dates:
        for r in by_date[d]:
            last[r["a"]] = r["bal"] or 0.0
        out.append({"date": d, "synced": round(sum(last.values()), 2), "accounts": dict(last)})
    return out


def period_changes(series: list[dict]) -> dict:
    """Change in the synced total vs 1/7/30 days ago (that exact day's snapshot) and month/year to date (the last
    snapshot on or before the previous month-end / year-end). The sheet-only total is constant, so it cancels out.

    Like with like: only accounts that already had a balance at the start of the period count. An account linked
    since then would add its whole opening balance as a "gain" (the money existed before, it just wasn't
    synced), so it is left out and reported: "excluded" (how many) and "excluded_total" (their balance today)."""
    changes = {}
    if not series:
        return changes
    cur_synced = series[-1]["synced"]
    now = series[-1].get("accounts")

    def change(base: dict) -> dict:
        then = base.get("accounts")
        if now is None or then is None:  # totals only
            return {"delta": round(cur_synced - base["synced"], 2), "vs": base["date"]}
        new = [a for a, bal in now.items() if a not in then and abs(bal) >= 0.005]
        out = {"delta": round(sum(now.get(a, 0.0) - bal for a, bal in then.items()), 2), "vs": base["date"]}
        if new:
            out.update(excluded=len(new), excluded_total=round(sum(now[a] for a in new), 2))
        return out
    latest = date.fromisoformat(series[-1]["date"])
    note = f"history starts {series[0]['date']}"
    for n in (1, 7, 30):
        target = (latest - timedelta(days=n)).isoformat()
        past = [s for s in series if s["date"] <= target]
        if past and past[-1]["date"] == target:
            changes[str(n)] = change(past[-1])
        else:
            changes[str(n)] = {"delta": None, "vs": target, "note": note}
    for key, base in (("mtd", latest.replace(day=1) - timedelta(days=1)), ("ytd", date(latest.year - 1, 12, 31))):
        past = [s for s in series if s["date"] <= base.isoformat()]
        changes[key] = (change(past[-1]) if past
                        else {"delta": None, "vs": base.isoformat(), "note": note})
    return changes


# ---------- endpoints ----------
def line_source(line: dict) -> str:
    """live = synced balance, or read today straight from an exchange or blockchain; est = modelled or priced
    from typed-in amounts by valuations/ (metals at spot, home/car models, crypto with sheet quantities), or a
    reading from an earlier day; sheet = typed in. networth.valuation_from decides between the first two."""
    f = line["from"]
    return "live" if f.startswith("live") else "est" if f.startswith("estimate") else "sheet"


def input_ages(lines: list[dict], today: date | None = None) -> dict:
    """How old the headline's inputs are, for the note beside it: per source ("live", "est"), how many counted
    lines are from before today and the oldest of them. Empty when everything dated is from today. Typed-in sheet
    values carry no date and are not counted here (their badge already says so)."""
    today = today or date.today()
    out = {}
    for src in ("live", "est"):
        dated = [(l["as_of"][:10], l["line"]) for l in lines
                 if l.get("as_of") and abs(l.get("used") or 0) >= 0.005 and line_source(l) == src]
        old = sorted(x for x in dated if x[0] < today.isoformat())
        if old:
            out[src] = {"old": len(old), "of": len(dated), "oldest": old[0][0], "line": old[0][1],
                        "age_days": (today - date.fromisoformat(old[0][0])).days}
    return out


def in_synced_series(line: dict) -> bool:
    """True when the line is a SimpleFIN/Plaid account balance, i.e. already inside synced_series(). A valuation
    read live from an exchange is not: it has no daily account snapshots, so the trend carries it as a constant."""
    return line["synced"] if "synced" in line else line["from"].startswith("live")


def crypto_wallet_lines(line: dict, src: str) -> list[dict]:
    """The crypto total split into one allocation line per wallet, when the latest valuation is the number in use
    (so the parts add up to it); otherwise nothing. A wallet's dollar cash has its own balance-sheet row, so that
    row is relabelled "<wallet> cash" to tell it from the wallet's coins. Each wallet is marked on its own: live
    when today's run read every counted amount in it from a chain or the exchange, even if another wallet's
    typed-in amount makes the total an estimate."""
    crypto = api_crypto({}) if line["line"] in (CRYPTO_LINE, *CRYPTO_CASH_ROWS) else {"wallets": []}
    wallets = {w["wallet"]: w["value"] for w in crypto["wallets"] if abs(w["value"]) >= 0.005}
    read = {w["wallet"] for w in crypto["wallets"] if crypto.get("as_of") == date.today().isoformat()
            and all(h["qty_source"] in READ_QTY for h in w["holdings"] if h["counted"])}
    if line["line"] in CRYPTO_CASH_ROWS and line["line"] in wallets:
        return [{"line": f"{line['line']} cash", "value": round(line["used"], 2), "source": src}]
    if line["line"] != CRYPTO_LINE or abs(sum(wallets.values()) - line["used"]) >= 0.01 * max(len(wallets), 1):
        return []
    return [{"line": w, "value": v, "source": "live" if w in read else src} for w, v in wallets.items()]


def hidden_sections() -> list[str]:
    """Page sections (their ids) this ledger has nothing for: no spreadsheet, or no personal.json section."""
    sheet = personal.has_sheet()
    off = {"budget": not sheet, "cryptocard": not sheet and not personal.section("crypto"),
           "ira": not personal.section("inherited_ira"), "rentalcard": not personal.section("rental")}
    return [k for k, v in off.items() if v]


def api_summary(params) -> dict:
    nw = networth(refresh="refresh" in params)
    data = nw["data"]
    res = {"status": read_status(), "networth_error": nw["error"],
           "networth_fetched": datetime.fromtimestamp(nw["fetched"]).astimezone().isoformat() if nw["fetched"] else None,
           "cache_ttl_s": CACHE_TTL, "sheetless": not personal.has_sheet(), "hide": hidden_sections(),
           "connect": connect_state()}
    if not data:
        return res
    res["status"]["problems"] += data.get("warnings") or []  # things that may make the total wrong (networth.py)
    cats = {k: {"key": k, "label": lbl, "type": t, "value": 0.0, "live": 0.0, "est": 0.0, "sheet": 0.0, "lines": []}
            for k, lbl, t in CATEGORIES}
    for l in data["lines"]:
        if abs(l["used"] or 0) < 0.005:
            continue
        c = cats[categorize(l)]
        signed = l["used"] if l["type"] == "asset" else -l["used"]
        src = line_source(l)
        c["value"] += signed
        c[src] += signed
        c["lines"] += crypto_wallet_lines(l, src) or [{"line": l["line"], "value": round(signed, 2), "source": src}]
    for c in cats.values():
        c["lines"].sort(key=lambda x: -abs(x["value"]))
        for k in ("value", "live", "est", "sheet"):
            c[k] = round(c[k], 2)
    sheet_const = round(sum((l["used"] if l["type"] == "asset" else -l["used"])
                            for l in data["lines"] if not in_synced_series(l)), 2)
    changes = period_changes(synced_series())
    res.update(net_worth=data["net_worth"], assets=data["assets"], liabilities=data["liabilities"],
               sheet_net_worth=data.get("sheet_net_worth"), sheet_only_total=sheet_const,
               changes=changes, categories=list(cats.values()), input_ages=input_ages(data["lines"]))
    return res


def api_trend(params) -> dict:
    nw = networth()
    series = [{"date": s["date"], "synced": s["synced"]} for s in synced_series()]
    sheet_const = None
    if nw["data"]:
        sheet_const = round(sum((l["used"] if l["type"] == "asset" else -l["used"])
                                for l in nw["data"]["lines"] if not in_synced_series(l)), 2)
    for s in series:
        s["total"] = round(s["synced"] + sheet_const, 2) if sheet_const is not None else None
    first_daily = series[0]["date"] if series else None
    points = sheet_points(first_daily)
    return {"series": series, "sheet_only_total": sheet_const,
            "note": "Synced accounts from daily snapshots (custodial accounts, crypto exchange wallets and "
                    "superseded feeds excluded, as in networth.py) plus today's sheet-only total held constant.",
            "first_daily": first_daily, "sheet_points": points,
            **trend_prices([s["date"] for s in series] + [p["date"] for p in points])}


# where the valuations providers keep today's spot (valuations/crypto.py, valuations/metals.py detail JSON)
SPOT_VALUATIONS = {"BTC": (CRYPTO_LINE, "$.prices.BTC.usd", "$.prices.BTC.source"),
                   "XAU": ("Total metals value", "$.metals.gold.spot", "$.metals.gold.source"),
                   "XAG": ("Total metals value", "$.metals.silver.spot", "$.metals.silver.source"),
                   "XPT": ("Total metals value", "$.metals.platinum.spot", "$.metals.platinum.source")}


BENCH_DAILY_DAYS = 400  # benchmark overlays: one point per day this far back (covers the 1Y range), weekly before


def bench_dates(dates, today: str) -> list[str]:
    """Dates the benchmark overlays are drawn at: weekly from the first trend date, daily over the last
    BENCH_DAILY_DAYS, plus every trend date (so each net-worth point has an exact match to rebase on)."""
    if not dates:
        return []
    end = date.fromisoformat(today)
    daily_from = end - timedelta(days=BENCH_DAILY_DAYS)
    out, d = set(dates), date.fromisoformat(min(dates))
    while d <= end:
        out.add(d.isoformat())
        d += timedelta(days=7 if d < daily_from else 1)
    return sorted(x for x in out if x <= today)


def trend_prices(dates, today: str | None = None) -> dict:
    """USD per unit of every display unit in insights/prices.py (BTC, metals, rial, dinar), for showing values in it.
    prices: {sym: {date: usd}} for every trend date (series + sheet_points; that day's close or the nearest earlier
    one; a date before the first stored price is left out). spot: {sym: {usd, date, source}} = the latest price,
    from the prices table or, when newer, the valuations providers' live spot; None when neither has one.
    benchmarks: {dates: [...bench_dates], prices: {sym: [usd or None per date]}} for the trend's overlays (S&P 500,
    Nasdaq, gold, bitcoin) and every unit with a price history, so the page can price an overlay in the shown unit."""
    today = today or date.today().isoformat()
    dates = sorted(set(dates))
    grid = bench_dates(dates, today)
    out = {"prices": {}, "spot": {}, "benchmarks": {"dates": grid, "prices": {}}}
    for sym in price_hist.SYMBOLS + price_hist.BENCHMARKS:
        try:
            rows = q("SELECT date, usd, source FROM prices WHERE symbol = ? ORDER BY date", (sym,))
        except sqlite3.OperationalError:  # older DB without the table
            rows = []
        series = [(r["date"], r["usd"]) for r in rows]
        if sym not in price_hist.LATEST_ONLY:
            at = price_hist.asof(series, grid)
            out["benchmarks"]["prices"][sym] = [at.get(d) for d in grid]
        if sym in price_hist.BENCHMARKS:
            continue
        out["prices"][sym] = price_hist.asof(series, dates)
        best = {"usd": rows[-1]["usd"], "date": rows[-1]["date"], "source": rows[-1]["source"]} if rows else None
        if (best is None or best["date"] < today) and sym in SPOT_VALUATIONS:
            asset, usd_path, src_path = SPOT_VALUATIONS[sym]
            try:
                v = q("SELECT snapshot_date d, json_extract(detail, ?) usd, json_extract(detail, ?) src FROM valuations "
                      "WHERE asset = ? AND json_extract(detail, ?) IS NOT NULL ORDER BY snapshot_date DESC LIMIT 1",
                      (usd_path, src_path, asset, usd_path))
            except sqlite3.OperationalError:
                v = []
            if v and (best is None or v[0]["d"] > best["date"]):
                best = {"usd": float(v[0]["usd"]), "date": v[0]["d"], "source": f"valuations ({v[0]['src']})"}
        out["spot"][sym] = best
    return out


def sheet_points(before: str | None) -> list[dict]:
    """Past whole-net-worth points from the yearly balance sheets (insights/nw_history.py), ascending, only dates
    before the first daily snapshot. [] when the table is missing (older DB) or empty."""
    try:
        rows = q("SELECT date, ROUND(net_worth, 2) net_worth, source FROM networth_history "
                 "WHERE net_worth IS NOT NULL AND (? IS NULL OR date < ?) ORDER BY date", (before, before))
    except sqlite3.OperationalError:
        return []
    return rows


STALE_DAYS = 3  # a balance the bank last updated more than this many days ago is flagged stale


def freshness(r: dict, today: date | None = None) -> None:
    """Normalise r['as_of'] for display and add age_days/stale. The bank's own balance date when the feed gives one
    (SimpleFIN), else the day the sync saw it (Plaid's cached balances carry no timestamp)."""
    raw = r.get("as_of") or ""
    r["as_of"] = raw[:16].replace("T", " ") or r["snapshot_date"]
    try:
        seen = date.fromisoformat((raw or r["snapshot_date"] or "")[:10])
    except ValueError:
        r["age_days"], r["stale"] = None, False
        return
    r["age_days"] = ((today or date.today()) - seen).days
    r["stale"] = r["age_days"] > STALE_DAYS


SPARK_DAYS = 90  # how far back each account's trend line goes


def api_accounts(params) -> dict:
    rows = q("""SELECT account_id, account_name, institution, source, ROUND(balance,2) balance, as_of, snapshot_date,
                       COALESCE(kind,'unclassified') kind
                FROM v_accounts ORDER BY kind, ABS(balance) DESC""")
    hist: dict[str, list] = {}  # account -> [date, balance] per day, oldest first (the row's trend line and change)
    for b in q("""SELECT account_id, snapshot_date, CAST(balance AS REAL) bal FROM balances
                  WHERE snapshot_date >= date('now', ?) ORDER BY snapshot_date""", (f"-{SPARK_DAYS} day",)):
        hist.setdefault(b["account_id"], []).append([b["snapshot_date"], round(b["bal"], 2)])
    for r in rows:
        freshness(r)
        r["counted"] = r["kind"] in NW_KINDS
        r["spark"] = hist.get(r["account_id"], [])
    return {"accounts": rows, "stale_days": STALE_DAYS, "spark_days": SPARK_DAYS}


def api_holdings(params) -> dict:
    rows = q("""SELECT h.symbol, h.description, h.account_name, h.value, h.shares, h.price, h.source, h.snapshot_date
                FROM v_holdings_latest h LEFT JOIN account_kind k USING (account_id)
                WHERE TRIM(COALESCE(h.symbol,'')) <> '' AND COALESCE(k.kind,'') <> 'custodial'""")
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["symbol"], {"symbol": r["symbol"], "description": r["description"], "value": 0.0,
                                         "accounts": [], "sources": set(), "as_of": r["snapshot_date"]})
        a["value"] += r["value"] or 0
        if abs(r["value"] or 0) >= 0.01:
            a["accounts"].append(re.sub(r"\s*\([0-9a-f-]+\)$", "", r["account_name"] or ""))
        a["sources"].add(r["source"])
        a["as_of"] = min(a["as_of"], r["snapshot_date"])
    total = sum(a["value"] for a in agg.values())
    out = sorted((a for a in agg.values() if abs(a["value"]) >= 0.01), key=lambda a: -a["value"])
    for a in out:
        a["value"] = round(a["value"], 2)
        a["pct"] = round(100 * a["value"] / total, 2) if total else 0
        a["accounts"] = sorted(set(a["accounts"]))
        a["sources"] = sorted(a["sources"])
    return {"holdings": out, "total": round(total, 2), "count": len(out),
            "note": "Excludes custodial accounts and blank-symbol cash sweeps. SimpleFIN has no cost basis, so no gains. "
                    "Manual holdings are as of their last manual entry."}


def api_crypto(params) -> dict:
    """Every crypto holding from the latest valuations/crypto.py run, grouped by wallet and by coin (each coin
    with the wallets it is kept in)."""
    rows = q("SELECT snapshot_date, value, detail, fetched_at FROM v_valuations_latest "
             "WHERE asset = ?", (CRYPTO_LINE,))
    if not rows:
        return {"total": None, "wallets": [], "coins": [], "transfers": crypto_transfers()}
    r = rows[0]
    d = json.loads(r["detail"] or "{}")
    wallets: dict[str, dict] = {}
    for h in d.get("holdings") or []:
        value, qty = h.get("value") or 0, h.get("qty") or 0
        if abs(value) < 0.01 and not (h.get("price") is None and qty > 0):  # dust; keep an unpriced coin visible
            continue
        w = wallets.setdefault(h["wallet"], {"wallet": h["wallet"], "value": 0.0, "holdings": []})
        if h.get("counted", True):
            w["value"] += value
        w["holdings"].append({"coin": h["coin"], "qty": qty, "price": h.get("price"), "value": round(value, 2),
                              "qty_source": h.get("qty_source"), "counted": h.get("counted", True),
                              "why_not_counted": h.get("why_not_counted")})
    out = sorted(wallets.values(), key=lambda w: -w["value"])
    for w in out:
        w["value"] = round(w["value"], 2)
        w["holdings"].sort(key=lambda h: (not h["counted"], -h["value"]))
    total = r["value"] or 0
    kept: dict[str, list] = {}  # coin -> the wallets holding it (counted holdings only), largest first
    for w in out:
        for h in w["holdings"]:
            if h["counted"]:
                kept.setdefault(h["coin"], []).append({"wallet": w["wallet"], "qty": h["qty"], "value": h["value"],
                                                       "qty_source": h["qty_source"]})
    coins = sorted(({"coin": c, "qty": v.get("qty"), "price": v.get("price"), "value": round(v.get("value") or 0, 2),
                     "pct": round(100 * (v.get("value") or 0) / total, 2) if total else 0,
                     "where": sorted(kept.get(c, []), key=lambda x: -x["value"])}
                    for c, v in (d.get("by_coin") or {}).items() if abs(v.get("value") or 0) >= 0.01),
                   key=lambda c: -c["value"])
    btc_price = (d.get("by_coin") or {}).get("BTC", {}).get("price")
    return {"total": round(total, 2), "as_of": r["snapshot_date"], "fetched_at": r["fetched_at"],
            "sheet_total": d.get("sheet_total"), "wallets": out, "coins": coins, "warnings": d.get("warnings") or [],
            "btc": btc_locations(out, btc_price), "transfers": crypto_transfers()}


# Spot bitcoin funds (Grayscale's Mini Trust trades as "BTC"); held in brokerage/IRA accounts, not wallets.
BTC_FUNDS = {"FBTC", "IBIT", "GBTC", "BTC", "BITB", "ARKB", "HODL", "BRRR", "EZBC", "BTCO", "BTCW"}
EXCHANGES = {"Gemini", "Coinbase", "Kraken", "Binance", "Fidelity Crypto", "Cash App", "Strike", "River"}


def btc_locations(wallets: list[dict], btc_price: float | None) -> dict:
    """Where Ian's bitcoin is: wallets (self-custody or exchange) plus bitcoin funds in brokerage/IRA accounts,
    as BTC-equivalent at today's price (fund value / BTC price; close enough, the funds track spot)."""
    rows = [{"where": w["wallet"], "type": "exchange" if w["wallet"] in EXCHANGES else "self-custody",
             "btc": h["qty"], "value": h["value"], "source": h["qty_source"]}
            for w in wallets for h in w["holdings"] if h["coin"] == "BTC" and h["counted"] and h["qty"]]
    funds = q(f"""SELECT h.account_name, h.symbol, SUM(h.value) value, MIN(h.snapshot_date) as_of
                  FROM v_holdings_latest h JOIN account_kind k USING (account_id)
                  WHERE h.symbol IN ({",".join("?" * len(BTC_FUNDS))}) AND k.kind IN ('investment', 'retirement')
                    AND h.value > 0
                  GROUP BY h.account_name, h.symbol""", sorted(BTC_FUNDS))
    for f in funds:
        rows.append({"where": re.sub(r"\s*\([0-9a-f]{8}-[0-9a-f-]+\)$", "", f["account_name"] or ""), "symbol": f["symbol"],
                     "type": "fund", "btc": f["value"] / btc_price if btc_price else None,
                     "value": round(f["value"], 2), "source": "fund", "as_of": f["as_of"]})
    rows.sort(key=lambda x: -x["value"])
    total_value = sum(x["value"] for x in rows)
    total_btc = sum(x["btc"] or 0 for x in rows)
    for x in rows:
        x["pct"] = round(100 * x["value"] / total_value, 2) if total_value else 0
    by_type = {}
    for x in rows:
        t = by_type.setdefault(x["type"], {"type": x["type"], "btc": 0.0, "value": 0.0})
        t["btc"] += x["btc"] or 0
        t["value"] += x["value"]
    types = sorted(by_type.values(), key=lambda t: -t["value"])
    for t in types:
        t["pct"] = round(100 * t["value"] / total_value, 2) if total_value else 0
        t["value"] = round(t["value"], 2)
    return {"price": btc_price, "btc": total_btc, "value": round(total_value, 2), "locations": rows, "by_type": types}


def crypto_transfers() -> list[dict]:
    """Every crypto send/receive with its memo and USD value on the day (insights/crypto_transfers.py)."""
    try:
        return q("""SELECT date, wallet, coin, direction, amount, fee, address, counterparty, memo, txid, usd_value,
                           fee_usd, source FROM v_crypto_transfers ORDER BY date DESC""")
    except sqlite3.OperationalError:  # database from before the table existed
        return []


def api_cards(params) -> dict:
    """Owed is positive dollars owed (negative = a credit balance). The limit is the card's credit limit from the
    feed (Plaid balances.limit) or personal.json "credit_limits" (v_accounts.credit_limit); without one it is
    approximated as owed + the feed's available credit (SimpleFIN reports 0 for cards it has no figure for, so 0
    counts as unknown; pending charges can make the estimate a little off). limit_basis says which:
    plaid/simplefin (feed), manual, or estimate."""
    rows = q("""SELECT b.account_id, b.account_name, b.institution, b.source, ROUND(-b.bal,2) owed, b.available, b.as_of,
                       b.snapshot_date, a.credit_limit, a.credit_limit_source
                FROM v_balances_latest b JOIN v_accounts a USING (account_id, source)
                WHERE b.kind='credit' ORDER BY b.bal""")
    known_owed = known_limit = 0.0
    for r in rows:
        freshness(r)
        try:
            avail = float(r["available"]) if r["available"] not in (None, "") else None
        except ValueError:
            avail = None
        avail = avail if avail and avail > 0 else None
        limit, basis = r.pop("credit_limit"), r.pop("credit_limit_source")
        if limit is None and avail is not None:
            limit, basis = max(r["owed"], 0) + avail, "estimate"
        if limit is not None and avail is None:
            avail = max(limit - max(r["owed"], 0), 0)
        r["available"] = round(avail, 2) if avail is not None else None
        r["limit"] = round(limit, 2) if limit else None
        r["limit_basis"] = basis if limit else None
        r["util_pct"] = round(100 * max(r["owed"], 0) / limit, 1) if limit else None
        if limit:
            known_owed += max(r["owed"], 0)
            known_limit += limit
    return {"cards": rows, "total": round(sum(r["owed"] for r in rows), 2),
            "limit": round(known_limit, 2) if known_limit else None,
            "limit_cards": sum(1 for r in rows if r["limit"]),
            "available": round(known_limit - known_owed, 2) if known_limit else None,
            "util_pct": round(100 * known_owed / known_limit, 1) if known_limit else None}


def api_drafts(params) -> dict:
    con = db()
    try:
        fc = overdraft.forecast(con)
    finally:
        con.close()
    return {"accounts": [a for a in fc if a["events"]], "warnings": overdraft.warnings(fc),
            "horizon_days": overdraft.HORIZON, "buffer": overdraft.BUFFER}


_bud_lock = threading.Lock()
_bud = {"data": None, "fetched": 0.0, "error": None}


def budget_sheet() -> dict:
    """The income-sheet budget lines via gws, cached like networth(); a failed read keeps the stale copy."""
    with _bud_lock:
        if not personal.has_sheet():  # ledger without a spreadsheet: no budget, generic categories
            return dict(_bud)
        if _bud["data"] is None or time.time() - _bud["fetched"] >= CACHE_TTL:
            try:
                _bud.update(data=budget.sheet_lines(), fetched=time.time(), error=None)
            except Exception as e:
                msg = getattr(e, "stderr", "") or str(e)
                _bud["error"] = f"budget sheet read failed: {str(msg).strip()[-300:]}"
        return dict(_bud)


def api_budget(params) -> dict:
    sh = budget_sheet()
    res = {"error": sh["error"], "fetched": datetime.fromtimestamp(sh["fetched"]).astimezone().isoformat()
           if sh["fetched"] else None, "tab": budget.TAB, "off": not personal.has_sheet()}
    if not sh["data"]:
        return res
    this_m, last_m = budget.months()
    con = db()
    try:
        res["months"] = [budget.compute(con, sh["data"]["lines"], m, since=LEDGER_START, label=merchant)
                         for m in (this_m, last_m)]
    finally:
        con.close()
    res["sheet_total"] = sh["data"]["sheet_total"]
    return res


UNCATEGORIZED = "Other"  # spending no budget rule matches


def add_categories(rows: list[dict]) -> None:
    """Give each spend or refund row the budget line its merchant maps to (category = the line's item, group =
    its sheet category). Other classes (income, transfers, card payments, investments) have no budget line.
    Without a spreadsheet the category is the monthly email's generic one (insights/monthly_email.py)."""
    sh = budget_sheet()
    line_for = budget.categorizer(sh["data"]["lines"]) if sh["data"] else None
    generic = None if personal.has_sheet() else monthly_email.category_rules(
        personal.section("monthly_email").get("categories"))
    for r in rows:
        r["category"] = r["group"] = None
        if r["class"] not in ("spend", "refund"):
            continue
        if generic:
            r["category"] = monthly_email.categorize(r["description"], r["payee"], generic)
            continue
        line = line_for(r["description"], r["payee"]) if line_for else None
        r["category"] = line["item"] if line else UNCATEGORIZED
        r["group"] = line["category"] if line else None


RECENT_LIMIT = 200  # recent transactions sent to the page (it shows 30 until you search or filter)
ACCOUNT_TXN_LIMIT = 5000  # one account's full history (clicked on the page); more than any account has today


RUNBAL_KINDS = ("checking", "savings", "credit")  # balances that move only by their transactions


def running_balances(account_id: str | None = None) -> dict:
    """txn_id -> the account's balance right after that transaction, walked back from its latest synced balance.

    Posted rows of cash and card accounts only: a pending row is not in the balance yet, and an investment
    balance moves with the market. Brokerage cash sweeps leave the balance where it was. A transaction dated
    after the balance snapshot is not in that balance, so it gets none.
    """
    sweep = " OR ".join("l.description LIKE ?" for _ in overdraft.SWEEPS)
    rows = q(f"""SELECT l.txn_id, l.account_id, l.amt, ({sweep}) sweep, a.balance FROM v_ledger l
                 JOIN v_accounts a USING (account_id)
                 WHERE l.pending=0 AND l.kind IN ({",".join("?" * len(RUNBAL_KINDS))}) AND l.date<=a.snapshot_date
                   AND (? IS NULL OR l.account_id=?)
                 ORDER BY l.account_id, l.date DESC, l.first_seen DESC, l.txn_id""",
             (*overdraft.SWEEPS, *RUNBAL_KINDS, account_id, account_id))
    out, acct, later = {}, None, 0.0
    for r in rows:
        if r["account_id"] != acct:
            acct, later = r["account_id"], 0.0
        out[r["txn_id"]] = round(r["balance"] - later, 2)
        if not r["sweep"]:
            later += r["amt"]
    return out


def api_account_txns(params) -> dict:
    aid = (params.get("id") or [""])[0]
    acct = q("SELECT account_id, account_name, institution, kind, ROUND(balance,2) balance FROM v_accounts "
             "WHERE account_id=?", (aid,))
    if not acct:
        return {"error": "unknown account", "txns": []}
    txns = q("""SELECT txn_id, date, ROUND(amt,2) amt, description, payee, memo, class, pending FROM v_ledger
                WHERE account_id=? ORDER BY date DESC, first_seen DESC, txn_id LIMIT ?""", (aid, ACCOUNT_TXN_LIMIT + 1))
    bal = running_balances(aid)
    add_categories(txns)
    for r in txns:
        r["balance_after"] = bal.get(r.pop("txn_id"))
        r["merchant"] = merchant(r["description"], r["payee"])
        r["account_name"] = re.sub(r"\s*\([0-9a-f-]+\)$", "", acct[0]["account_name"] or "")
    posted = [r for r in txns[:ACCOUNT_TXN_LIMIT] if not r["pending"]]
    return {"account": acct[0], "txns": txns[:ACCOUNT_TXN_LIMIT], "truncated": len(txns) > ACCOUNT_TXN_LIMIT,
            "money_in": round(sum(r["amt"] for r in posted if r["amt"] > 0), 2),
            "money_out": round(sum(r["amt"] for r in posted if r["amt"] < 0), 2),
            "first": txns[min(len(txns), ACCOUNT_TXN_LIMIT) - 1]["date"] if txns else None}


def api_subscriptions(params) -> dict:
    con = db()
    try:
        rep = subscriptions.report(con)
    finally:
        con.close()
    for r in rep["subscriptions"] + rep["unusual"]:
        r["merchant"] = merchant(r["description"], r["payee"])
        r["account"] = re.sub(r"\s*\([0-9a-f-]+\)$", "", r["account"] or "")
    return rep


def api_rental(params) -> dict:
    con = db()
    try:
        return rental.summary(con, personal.section("rental"), LEDGER_START)
    finally:
        con.close()


def api_inherited_ira(params) -> dict:
    con = db()
    try:
        return inherited_ira.report(con)
    finally:
        con.close()


GENERIC_PAYEES = {"investment buy", "investment sell", "payment", "transfer", "deposit", "withdrawal"}


def merchant(desc: str, payee: str | None) -> str:
    s = (payee or "").strip()
    if not s or s.lower() in GENERIC_PAYEES:
        s = desc or s
    s = re.sub(r"\s+\(?cash\)?$", "", s, flags=re.I).strip()
    s = re.sub(r"^(direct debit|debit card purchase|pos debit)\s+", "", s, flags=re.I)
    s = re.sub(r"\s*[#*].*$", "", s)                 # ONLINE MKTPL*XYZ, GROCERY #123 ...
    s = re.sub(r"\s+\d[\d\s-]*.*$", "", s) if not re.match(r"^\d", s) else s  # trailing store numbers
    return re.sub(r"\s+", " ", s).strip().title() or "(unknown)"


def daily_spend(month: str, through: date) -> list:
    """Posted spend per day of `month` ("YYYY-MM"), index 0 = the 1st, up to `through` or the month's end.
    Days before LEDGER_START are None (no complete data), not 0."""
    first = date.fromisoformat(month + "-01")
    end = min(through, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1))
    by_day = {r["date"]: r["spent"] for r in q(
        """SELECT date, ROUND(-SUM(amt),2) spent FROM v_ledger WHERE class='spend' AND pending=0
           AND date BETWEEN ? AND ? AND date >= ? GROUP BY date""", (first.isoformat(), end.isoformat(), LEDGER_START))}
    days = [first + timedelta(days=i) for i in range((end - first).days + 1)]
    return [None if d.isoformat() < LEDGER_START else by_day.get(d.isoformat(), 0.0) for d in days]


def api_spending(params) -> dict:
    today = date.today()
    this_m = today.strftime("%Y-%m")
    last_m = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    # spend and income per month (income = class 'income' only: transfers, card payments, refunds and investment
    # moves are not income). A month is partial when complete data starts inside it, or it is this month.
    rows = q("""SELECT strftime('%Y-%m', date) month, class, ROUND(SUM(amt),2) total, COUNT(*) n FROM v_ledger
                WHERE class IN ('spend','income') AND pending=0 AND date >= ? GROUP BY month, class""", (LEDGER_START,))
    by_m: dict[str, dict] = {}
    for r in rows:
        m = by_m.setdefault(r["month"], {"month": r["month"], "spent": 0.0, "n": 0, "income": 0.0, "n_income": 0})
        if r["class"] == "spend":
            m["spent"], m["n"] = -r["total"], r["n"]
        else:
            m["income"], m["n_income"] = r["total"], r["n"]
    monthly = [by_m[k] for k in sorted(by_m)]
    for m in monthly:
        m["net"] = round(m["income"] - m["spent"], 2)
        m["partial"] = m["month"] == this_m or (m["month"] == LEDGER_START[:7] and LEDGER_START[8:] != "01")
    tops = {}
    for m in (this_m, last_m):
        rows = q("""SELECT description, payee, amt FROM v_ledger WHERE class='spend' AND pending=0
                    AND strftime('%Y-%m', date)=? AND date >= ?""", (m, LEDGER_START))
        agg: dict[str, list] = {}
        for r in rows:
            k = merchant(r["description"], r["payee"])
            a = agg.setdefault(k, [0.0, 0])
            a[0] -= r["amt"]
            a[1] += 1
        tops[m] = [{"merchant": k, "spent": round(v[0], 2), "n": v[1]}
                   for k, v in sorted(agg.items(), key=lambda kv: -kv[1][0])[:10]]
    recent = q("""SELECT txn_id, date, account_name, ROUND(amt,2) amt, description, payee, memo, class, pending
                  FROM v_ledger ORDER BY date DESC, first_seen DESC, txn_id LIMIT ?""", (RECENT_LIMIT,))
    bal = running_balances()
    add_categories(recent)
    for r in recent:
        r["balance_after"] = bal.get(r.pop("txn_id"))
        r["merchant"] = merchant(r["description"], r["payee"])
        r["account_name"] = re.sub(r"\s*\([0-9a-f-]+\)$", "", r["account_name"] or "")
    return {"monthly": monthly, "this_month": this_m, "last_month": last_m, "top": tops, "recent": recent,
            "since": LEDGER_START, "today": today.isoformat(),
            "daily": {this_m: daily_spend(this_m, today), last_m: daily_spend(last_m, today)},
            "note": f"class='spend', posted only, since {LEDGER_START} (complete data starts then; that month is partial)."}


ROUTES = {"/api/summary": api_summary, "/api/trend": api_trend, "/api/accounts": api_accounts,
          "/api/holdings": api_holdings, "/api/crypto": api_crypto, "/api/cards": api_cards, "/api/spending": api_spending,
          "/api/drafts": api_drafts, "/api/budget": api_budget, "/api/subscriptions": api_subscriptions,
          "/api/rental": api_rental, "/api/inherited_ira": api_inherited_ira, "/api/account_txns": api_account_txns}


# ---------- connecting SimpleFIN from the page ----------
# personal.json "dashboard": {"web_connect": true} lets a signed-in viewer paste a SimpleFIN setup token while the
# ledger has no access URL yet (and only then: an existing connection can't be replaced from the page). The token
# is claimed here and the first 90-day sync runs in the background. Off by default.
WEB_CONNECT = bool(_DASH.get("web_connect"))
SYNC = os.path.join(ROOT, "bin", "wealth-ledger-sync")
_conn_lock = threading.Lock()
_conn = {"running": False, "error": None}


def connect_state() -> dict | None:
    if not WEB_CONNECT:
        return None
    return {"needed": not os.path.exists(ACCESS_FILE), "running": _conn["running"], "error": _conn["error"]}


def _first_sync():
    try:
        r = subprocess.run([SYNC, "--days", "90"], capture_output=True, text=True, timeout=1800)
        if os.environ.get("WEALTH_LEDGER_SYNC_LOG"):  # same file the daily cron run appends to
            with open(os.path.expanduser(os.environ["WEALTH_LEDGER_SYNC_LOG"]), "a") as f:
                f.write(r.stdout + r.stderr)
        _conn["error"] = None if r.returncode == 0 else "The first sync had problems; see the warnings above."
        log(f"connect: first sync rc={r.returncode}")
    except Exception as e:
        _conn["error"] = "The first sync did not finish; it will be tried again at the next daily sync."
        log(f"connect: first sync failed: {e!r}")
    finally:
        _conn["running"] = False
        _nw["fetched"] = 0.0  # next summary recomputes the net worth


def start_connect(token: str) -> tuple[int, dict]:
    """Claim a SimpleFIN setup token and start the first sync. (HTTP status, body)."""
    if not WEB_CONNECT:
        return 404, {"error": "not found"}
    sys.path.insert(0, os.path.join(ROOT, "simplefin")) if os.path.join(ROOT, "simplefin") not in sys.path else None
    import simplefin_sync
    with _conn_lock:
        if os.path.exists(ACCESS_FILE) or _conn["running"]:
            return 409, {"error": "Already connected."}
        try:
            u = urlparse(base64.b64decode("".join(token.split()), validate=True).decode())
            host = (u.hostname or "").lower()
            if u.scheme != "https" or not (host == "simplefin.org" or host.endswith(".simplefin.org")):
                raise ValueError
        except Exception:
            return 400, {"error": "That doesn't look like a SimpleFIN setup token. Copy the whole token and try again."}
        try:
            simplefin_sync.claim("".join(token.split()), Path(ACCESS_FILE))
        except simplefin_sync.SimpleFinError as e:
            log(f"connect: claim failed: {e}")
            return 400, {"error": f"SimpleFIN refused the token ({e}). Create a new setup token and try again."}
        except Exception as e:
            log(f"connect: claim failed: {e!r}")
            return 502, {"error": "Could not reach SimpleFIN. Try again in a minute."}
        _conn.update(running=True, error=None)
        threading.Thread(target=_first_sync, daemon=True).start()
        log("connect: SimpleFIN token claimed; first sync started")
        return 200, {"ok": True}


# ---------- auth ----------
# Every path except /login, /logout and /robots.txt needs a valid session, including loopback requests
# (cloudflared connects from 127.0.0.1). Credentials: AUTH_FILE (0600, scrypt hash + session-signing secret).
CONF_DIR = os.path.expanduser(os.environ.get("WEALTH_LEDGER_CONFIG_DIR") or "~/.config/wealth-ledger")
AUTH_FILE = os.path.join(CONF_DIR, "dashboard_auth.json")
SESSIONS_FILE = os.path.join(CONF_DIR, "dashboard_sessions.json")
ACCESS_FILE = os.path.expanduser(os.environ.get("WEALTH_LEDGER_ACCESS") or os.path.join(CONF_DIR, "simplefin_access_url"))
ICON_DIR = os.path.join(CONF_DIR, "icons")  # <institution slug>.png, shown next to accounts (see README)
ICON_PATH = re.compile(r"/icons/([a-z0-9]+(?:-[a-z0-9]+)*)\.png")
COOKIE = "ctd_session"
SESSION_TTL = 365 * 86400  # sliding: any visit after RENEW_AFTER pushes expiry a full TTL out
RENEW_AFTER = 86400
SCRYPT = {"n": 2 ** 17, "r": 8, "p": 1}
SCRYPT_MAXMEM = 256 * 1024 * 1024
IP_MAX_FAILS, IP_WINDOW = 5, 15 * 60
GLOBAL_MAX_FAILS, GLOBAL_WINDOW, GLOBAL_LOCK = 50, 60 * 60, 15 * 60
MAX_SESSIONS = 100
COOKIE_RE = re.compile(r"v1\.[A-Za-z0-9_-]{43}\.\d{10}\.[A-Za-z0-9_-]{43}")


# ---------- last good answers ----------
# The page shows the previous numbers at once and swaps in fresh ones as they arrive (some requests wait ten seconds
# on a Google Sheet read). So every successful answer to a page-load request is kept: in memory, and as one 0600
# file per route under this instance's config dir (never the repo), so a restart doesn't lose them. GET /api/last
# hands them all back without computing anything. Nothing is kept in the browser.
LAST_DIR = os.path.join(CONF_DIR, "dashboard_last")
LAST_KEYS = ("summary", "trend", "accounts", "holdings", "crypto", "cards", "spending", "drafts", "budget",
             "subscriptions", "rental", "inherited_ira")
_last_lock = threading.Lock()
_last: dict[str, tuple[float, bytes]] | None = None  # route → (when it was answered, its JSON); None = disk not read yet


def _last_loaded() -> dict[str, tuple[float, bytes]]:
    """The kept answers, read from disk the first time. Call with _last_lock held."""
    global _last
    if _last is None:
        _last = {}
        for key in LAST_KEYS:
            path = os.path.join(LAST_DIR, key + ".json")
            try:
                with open(path, "rb") as f:
                    body = f.read()
                if isinstance(json.loads(body), dict):  # a torn or hand-edited file is ignored
                    _last[key] = (os.path.getmtime(path), body)
            except (OSError, ValueError):
                pass
    return _last


def worth_keeping(key: str, res: dict) -> bool:
    """An answer with nothing to show must not replace one that had numbers: the summary and trend when networth.py
    failed and there is no earlier copy in memory (right after a restart), the budget when its sheet can't be read."""
    if key in ("summary", "trend"):
        return not (_nw["data"] is None and _nw["error"])
    if key == "budget":
        return not (res.get("error") and "months" not in res)
    return True


def remember(key: str, res: dict, body: bytes) -> None:
    """Keep a successful answer for /api/last. Never raises: the request it rides on is still answered."""
    if key not in LAST_KEYS or not worth_keeping(key, res):
        return
    with _last_lock:
        last, now, path = _last_loaded(), time.time(), os.path.join(LAST_DIR, key + ".json")
        same = key in last and last[key][1] == body
        last[key] = (now, body)
        try:
            if same and os.path.exists(path):
                os.utime(path, (now, now))  # unchanged: only the time it was last true moves
                return
            os.makedirs(LAST_DIR, mode=0o700, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            os.replace(tmp, path)
        except OSError as e:
            log(f"last good answers: can't save {key}: {e!r}")


def last_good() -> bytes:
    """Body of GET /api/last: {route: {"saved": epoch seconds, "data": that route's last answer}}."""
    with _last_lock:
        last = dict(_last_loaded())
    return b"{" + b",".join(b'"%s":{"saved":%s,"data":%s}' % (k.encode(), str(round(t, 3)).encode(), body)
                           for k, (t, body) in last.items()) + b"}"


def icon_bytes(slug: str) -> bytes | None:
    """The PNG for an institution slug, or None when there is no such icon."""
    try:
        with open(os.path.join(ICON_DIR, f"{slug}.png"), "rb") as f:
            body = f.read(200_000)
    except OSError:
        return None
    return body if body.startswith(b"\x89PNG") else None


def log(msg: str):
    print(msg, file=sys.stderr, flush=True)


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _kdf(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32)


def _write_private(path: str, data: dict):
    """Atomically write JSON with mode 0600."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Auth:
    def __init__(self):
        self.lock = threading.Lock()        # credentials + session store
        self.login_lock = threading.Lock()  # serializes password checks so the limits can't be raced
        self._key = None
        self._creds = None
        self.sessions: dict[str, int] = {}  # sha256(session id) -> expiry
        self.ip_fails: dict[str, deque] = {}
        self.global_fails: deque = deque()
        self.global_locked_until = 0.0

    # credentials (reloaded when the file changes, so set-password needs no restart; fail closed)
    def creds(self) -> dict | None:
        with self.lock:
            try:
                st = os.stat(AUTH_FILE)
                key = (st.st_mtime_ns, st.st_ino, st.st_size, st.st_mode)
            except OSError as e:
                st, key = None, ("err", e.errno)
            if key == self._key:
                return self._creds
            self._key, self._creds = key, None
            if st is None:
                log(f"auth: {AUTH_FILE} unreadable ({os.strerror(key[1])}); all logins refused. "
                    f"Run: server.py set-password")
            elif st.st_mode & 0o077:
                log(f"auth: {AUTH_FILE} has mode {oct(st.st_mode & 0o777)}; must be 0600. All logins refused.")
            else:
                try:
                    with open(AUTH_FILE) as f:
                        c = json.load(f)
                    creds = {"username": str(c["username"]), "salt": _b64d(c["salt"]), "hash": _b64d(c["hash"]),
                             "n": int(c["n"]), "r": int(c["r"]), "p": int(c["p"]),
                             "secret": bytes.fromhex(c["session_secret"])}
                    if len(creds["secret"]) < 32 or len(creds["hash"]) != 32 or len(creds["salt"]) < 16:
                        raise ValueError("short secret/hash/salt")
                    self._creds = creds
                except Exception as e:
                    log(f"auth: {AUTH_FILE} invalid ({type(e).__name__}); all logins refused")
            self.sessions = self._load_sessions()
            return self._creds

    def _secret_id(self) -> str:
        return hashlib.sha256(b"sid" + self._creds["secret"]).hexdigest()[:16] if self._creds else ""

    def _load_sessions(self) -> dict:
        if not self._creds:
            return {}
        try:
            with open(SESSIONS_FILE) as f:
                d = json.load(f)
            if d.get("secret_id") != self._secret_id():  # password rotated: old sessions are void
                return {}
            now = time.time()
            return {k: int(v) for k, v in d["sessions"].items() if int(v) > now}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    def _save_sessions(self):  # caller holds self.lock
        now = time.time()
        live = sorted(((k, v) for k, v in self.sessions.items() if v > now), key=lambda kv: kv[1])
        self.sessions = dict(live[-MAX_SESSIONS:])
        try:
            _write_private(SESSIONS_FILE, {"secret_id": self._secret_id(), "sessions": self.sessions})
        except OSError as e:
            log(f"auth: could not save sessions: {e}")

    # sessions: cookie = v1.<sid>.<exp>.<HMAC-SHA256(secret, "v1.<sid>.<exp>")>, sid must also be in the store
    @staticmethod
    def _sign(secret: bytes, msg: str) -> str:
        return _b64e(hmac.new(secret, msg.encode(), hashlib.sha256).digest())

    @staticmethod
    def _sid_key(sid: str) -> str:
        return hashlib.sha256(sid.encode()).hexdigest()

    def new_session(self) -> str:
        c = self.creds()
        if not c:
            raise RuntimeError("credentials vanished during login")
        sid, exp = _b64e(secrets.token_bytes(32)), int(time.time()) + SESSION_TTL
        msg = f"v1.{sid}.{exp}"
        with self.lock:
            self.sessions[self._sid_key(sid)] = exp
            self._save_sessions()
        return f"{msg}.{self._sign(c['secret'], msg)}"

    def check(self, value: str) -> str | None:
        """Session-store key if the cookie value is a valid, unexpired, unrevoked session; else None."""
        c = self.creds()
        if not c or not COOKIE_RE.fullmatch(value or ""):
            return None
        _, sid, exp, sig = value.split(".")
        if not hmac.compare_digest(self._sign(c["secret"], f"v1.{sid}.{exp}").encode(), sig.encode()):
            return None
        now = time.time()
        if int(exp) <= now:
            return None
        k = self._sid_key(sid)
        with self.lock:
            stored = self.sessions.get(k)
        return k if stored and stored > now else None

    def renew(self, value: str) -> str | None:
        """Fresh cookie (same session, expiry reset to a full TTL) once the session is RENEW_AFTER old; else None."""
        k, c = self.check(value), self.creds()
        if not k or not c:
            return None
        _, sid, exp, _ = value.split(".")
        now = int(time.time())
        if int(exp) > now + SESSION_TTL - RENEW_AFTER:
            return None
        exp = now + SESSION_TTL
        with self.lock:
            if k not in self.sessions:  # revoked meanwhile
                return None
            self.sessions[k] = exp
            self._save_sessions()
        msg = f"v1.{sid}.{exp}"
        return f"{msg}.{self._sign(c['secret'], msg)}"

    def revoke(self, values: list[str]):
        keys = [k for k in (self.check(v) for v in values) if k]
        if keys:
            with self.lock:
                for k in keys:
                    self.sessions.pop(k, None)
                self._save_sessions()

    # login + brute-force limits (in memory; a service restart clears them)
    @staticmethod
    def _bucket(ip: str) -> str:
        """Rate-limit key: the address, or its /64 for IPv6 (one host usually owns the whole /64)."""
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return ip
        return str(ipaddress.ip_network(f"{a}/64", strict=False)) if a.version == 6 else str(a)

    def _retry_after(self, ip: str, now: float) -> int:
        if now < self.global_locked_until:
            return int(self.global_locked_until - now) + 1
        ip = self._bucket(ip)
        q = self.ip_fails.get(ip)
        if q:
            while q and q[0] <= now - IP_WINDOW:
                q.popleft()
            if len(q) >= IP_MAX_FAILS:
                return int(q[0] + IP_WINDOW - now) + 1
            if not q:
                del self.ip_fails[ip]
        return 0

    def _record_failure(self, ip: str, now: float):
        q = self.ip_fails.setdefault(self._bucket(ip), deque())
        q.append(now)
        g = self.global_fails
        g.append(now)
        while g and g[0] <= now - GLOBAL_WINDOW:
            g.popleft()
        msg = (f"auth: login FAILED ip={ip} ({len(q)}/{IP_MAX_FAILS} for this ip in 15 min, "
               f"{len(g)}/{GLOBAL_MAX_FAILS} total in 1 h)")
        if len(q) >= IP_MAX_FAILS:
            msg += "; ip locked out for 15 min"
        if len(g) >= GLOBAL_MAX_FAILS:
            self.global_locked_until = now + GLOBAL_LOCK
            g.clear()
            msg += "; GLOBAL cap hit, all logins locked for 15 min"
        log(msg)
        if len(self.ip_fails) > 10000:
            for k in [k for k, v in self.ip_fails.items() if not v or v[-1] <= now - IP_WINDOW]:
                del self.ip_fails[k]

    def login(self, ip: str, username: str, password: str) -> tuple[str, object]:
        """('ok', cookie) | ('bad', None) | ('locked', retry_after_s)"""
        if not self.login_lock.acquire(timeout=15):
            return "locked", 5
        try:
            now = time.time()
            retry = self._retry_after(ip, now)
            if retry:
                log(f"auth: login refused (locked out) ip={ip} retry_after={retry}s")
                return "locked", retry
            c = self.creds()
            if c:
                pw_ok = hmac.compare_digest(_kdf(password, c["salt"], c["n"], c["r"], c["p"]), c["hash"])
                user_ok = hmac.compare_digest(username.encode(), c["username"].encode())
            else:  # no usable credentials: same work, always fail
                _kdf(password, b"\0" * 16, **SCRYPT)
                pw_ok = user_ok = False
            if pw_ok and user_ok:
                self.ip_fails.pop(self._bucket(ip), None)
                log(f"auth: login ok ip={ip}")
                return "ok", self.new_session()
            self._record_failure(ip, now)
            return "bad", None
        finally:
            self.login_lock.release()


AUTH = Auth()


def gen_passphrase() -> str:
    """6 groups of 4 random lowercase letters (~113 bits), easy to type on a phone."""
    return "-".join("".join(secrets.choice(string.ascii_lowercase) for _ in range(4)) for _ in range(6))


def cli_set_password(argv: list[str]):
    ap = argparse.ArgumentParser(prog="server.py set-password",
                                 description="Set or rotate the dashboard password. Signs out every session; "
                                             "the running server picks it up on its next request.")
    ap.add_argument("--username", help="default: keep the current one, else personal.json dashboard.default_username")
    ap.add_argument("--generate", action="store_true", help="generate a random passphrase and print it once")
    a = ap.parse_args(argv)
    username = a.username
    if not username:
        try:
            with open(AUTH_FILE) as f:
                username = json.load(f)["username"]
        except (OSError, ValueError, KeyError):
            username = _DASH.get("default_username") or "admin"
    if a.generate:
        pw = gen_passphrase()
    else:
        pw = getpass.getpass("New password: ")
        if pw != getpass.getpass("Repeat: "):
            sys.exit("passwords don't match; nothing changed")
        if len(pw) < 12:
            sys.exit("use at least 12 characters; nothing changed")
    salt = secrets.token_bytes(16)
    _write_private(AUTH_FILE, {
        "username": username, "kdf": "scrypt", **SCRYPT, "salt": _b64e(salt), "hash": _b64e(_kdf(pw, salt, **SCRYPT)),
        "session_secret": secrets.token_bytes(32).hex(), "updated": datetime.now().astimezone().isoformat(timespec="seconds")})
    try:
        os.remove(SESSIONS_FILE)
    except FileNotFoundError:
        pass
    print(f"Password set for '{username}' in {AUTH_FILE}; all sessions signed out.")
    if a.generate:
        print(f"New password (shown once): {pw}")


# ---------- http ----------
ROBOTS = b"User-agent: *\nDisallow: /\n"
CHARTJS = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"
CSP_BASE = "default-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#111110">
<title>Sign in · Wealth Ledger</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='4' fill='%232a78d6'/%3E%3Cpath d='M3 11l3-3 2 2 5-5' stroke='white' stroke-width='1.8' fill='none'/%3E%3C/svg%3E">
<style>
:root {
  color-scheme: dark;
  --bg: #111110; --surface: #1a1a19; --field: #111110; --border: #2e2e2c; --border-2: #45453f;
  --text: #f0efec; --text-2: #c3c2b7; --muted: #96958e; --accent: #5a9bef; --btn: #2b6fc9;
  --btn-hover: #2563b6; --btn-text: #ffffff; --pos: #4cc77f; --neg: #f07068; --err-bg: #2c1614; --warn: #f2c35b;
}
:root[data-theme="rose"] {
  --bg: #151012; --surface: #1f1719; --field: #151012; --border: #3b2c31; --border-2: #574349;
  --text: #f6eef0; --text-2: #d6c3c8; --muted: #a59197; --accent: #ec9cb4; --btn: #e79ab1;
  --btn-hover: #f0adc1; --btn-text: #2a1118; --pos: #5fd08f; --neg: #ff7759; --err-bg: #33161a; --warn: #f2c35b;
}
:root[data-theme="amethyst"] {
  --bg: #121019; --surface: #1a1724; --field: #121019; --border: #353046; --border-2: #4f4866;
  --text: #f1eef9; --text-2: #c9c3db; --muted: #9a93ae; --accent: #b9a6f5; --btn: #b3a0f0;
  --btn-hover: #c5b6f7; --btn-text: #1b1433; --pos: #56cc8b; --neg: #f47a72; --err-bg: #30171c; --warn: #f2c35b;
}
:root[data-theme="plum"] {
  --bg: #160e15; --surface: #21151f; --field: #160e15; --border: #41293c; --border-2: #5e3f57;
  --text: #f7ecf4; --text-2: #d9c2d3; --muted: #a98ea1; --accent: #e58bc2; --btn: #b83d80;
  --btn-hover: #a53572; --btn-text: #ffffff; --pos: #5fd08f; --neg: #ff7a5c; --err-bg: #36161a; --warn: #f2c35b;
}
:root[data-theme="sage"] {
  --bg: #0e1311; --surface: #151c19; --field: #0e1311; --border: #2c3932; --border-2: #445549;
  --text: #edf3ef; --text-2: #bfcdc5; --muted: #8b9b92; --accent: #93d1b3; --btn: #8fcdae;
  --btn-hover: #a4dcc0; --btn-text: #0e2219; --pos: #62d68a; --neg: #f47a6e; --err-bg: #2d1917; --warn: #f2c35b;
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--bg); color: var(--text); min-height: 100vh; min-height: 100dvh; display: flex; flex-direction: column;
  align-items: center; justify-content: center; padding: 24px 16px 10vh;
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, "Helvetica Neue", Arial, sans-serif;
  -webkit-text-size-adjust: 100%; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 32px 28px 26px; width: 100%;
  max-width: 384px; }
.mark { display: block; width: 36px; height: 36px; margin: 0 0 18px; }
.mark rect { fill: var(--btn); }
.mark path { stroke: var(--btn-text); }
h1 { font-size: 22px; line-height: 1.2; font-weight: 650; letter-spacing: -.015em; margin: 0 0 4px; }
.sub { font-size: 14px; color: var(--text-2); margin: 0 0 24px; }
label { display: block; font-size: 13px; font-weight: 500; color: var(--text-2); margin: 0 0 6px; }
.field { position: relative; margin: 0 0 16px; }
input { display: block; width: 100%; height: 48px; font: inherit; font-size: 16px; color: var(--text); background: var(--field);
  border: 1px solid var(--border-2); border-radius: 10px; padding: 0 13px; margin: 0; appearance: none; -webkit-appearance: none;
  transition: border-color .12s, box-shadow .12s; }
input:hover { border-color: var(--muted); }
input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 24%, transparent); }
#p { padding-right: 52px; font-variant-ligatures: none; }
.reveal { position: absolute; top: 1px; right: 1px; bottom: 1px; width: 48px; display: flex; align-items: center; justify-content: center;
  padding: 0; border: 0; border-radius: 9px; background: none; color: var(--muted); cursor: pointer; }
.reveal:hover { color: var(--text); }
.reveal[hidden] { display: none; }
.reveal .slash { display: none; }
.reveal[aria-pressed="true"] { color: var(--accent); }
.reveal[aria-pressed="true"] .slash { display: inline; }
.reveal:focus-visible, .go:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.caps { font-size: 12.5px; color: var(--warn); margin: -8px 0 14px; }
.caps[hidden] { display: none; }
.go { display: block; width: 100%; height: 48px; font: inherit; font-weight: 600; color: var(--btn-text); background: var(--btn); border: 0;
  border-radius: 10px; padding: 0 16px; margin: 8px 0 0; cursor: pointer; transition: background-color .12s; }
.go:hover { background: var(--btn-hover); }
.go:disabled { opacity: .65; cursor: progress; }
.err { display: flex; gap: 9px; align-items: flex-start; background: var(--err-bg); color: var(--neg);
  border: 1px solid color-mix(in srgb, var(--neg) 28%, transparent); border-radius: 10px; padding: 10px 12px; margin: 0 0 18px;
  font-size: 14px; line-height: 1.4; }
.err svg { flex: none; margin-top: 2px; }
.foot { font-size: 12.5px; color: var(--muted); text-align: center; margin: 18px 0 0; }
/* under the card: closes of a few broad benchmarks (a fixed list; nothing from the ledger), then the credit */
.mkt { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr); gap: 6px; width: 100%; max-width: 384px;
  margin: 22px 0 0; font-variant-numeric: tabular-nums; }
.mkt > div { display: flex; flex-direction: column; align-items: center; gap: 2px; min-width: 0; white-space: nowrap; }
.mkt .n { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); }
.mkt svg { width: 14px; height: 14px; flex: none; }
.mkt b { font-size: 13.5px; font-weight: 600; color: var(--text); }
.mkt i { font-style: normal; font-size: 11.5px; color: var(--muted); }
.mkt i.up { color: var(--pos); }
.mkt i.down { color: var(--neg); }
.upd { font-size: 11.5px; color: var(--muted); margin: 12px 0 0; font-variant-numeric: tabular-nums; }
.by { font-size: 12px; color: var(--muted); margin: 6px 0 0; }
@media (max-width: 420px) { .card { padding: 26px 20px 22px; } }
@media (prefers-reduced-motion: reduce) { input, .go { transition: none; } }
</style>
<script>
try { var t = localStorage.getItem("wl.theme"); if (/^(graphite|rose|amethyst|plum|sage)$/.test(t)) document.documentElement.dataset.theme = t; } catch (e) { /* storage blocked */ }
document.querySelector('meta[name="theme-color"]').content = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
</script>
</head>
<body>
<main class="card">
  <svg class="mark" viewBox="0 0 16 16" aria-hidden="true"><rect width="16" height="16" rx="4"/><path d="M3 11l3-3 2 2 5-5" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>
  <h1>Wealth Ledger</h1>
  <p class="sub">Sign in to your private dashboard.</p>
  {{error}}
  <form method="post" action="/login" autocomplete="on">
    <label for="u">Username</label>
    <div class="field"><input id="u" name="username" type="text" value="{{username}}" autocomplete="username" autocapitalize="none" autocorrect="off" spellcheck="false" maxlength="256" required autofocus></div>
    <label for="p">Password</label>
    <div class="field"><input id="p" name="password" type="password" autocomplete="current-password" autocapitalize="none" autocorrect="off" spellcheck="false" maxlength="1024" required>
      <button type="button" class="reveal" id="t" aria-pressed="false" aria-controls="p" aria-label="Show password" title="Show password" hidden><svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/><path class="slash" d="M4 4l16 16"/></svg></button></div>
    <p class="caps" id="caps" role="status" hidden>Caps Lock is on.</p>
    <button type="submit" class="go" id="go">Sign in</button>
  </form>
  <p class="foot">You stay signed in on this device until you log out.</p>
</main>
{{markets}}
<p class="by">Built by Keolabs</p>
<script>
"use strict";
(function () {
  var f = document.forms[0], u = document.getElementById("u"), p = document.getElementById("p"),
      t = document.getElementById("t"), caps = document.getElementById("caps"), go = document.getElementById("go");
  function reveal(show) {
    p.type = show ? "text" : "password";
    t.setAttribute("aria-pressed", String(show));
    t.title = show ? "Hide password" : "Show password";
    t.setAttribute("aria-label", t.title);
  }
  t.hidden = false;
  t.addEventListener("click", function () { reveal(p.type === "password"); p.focus(); });
  function capsLock(e) { if (e.getModifierState) caps.hidden = !e.getModifierState("CapsLock"); }
  p.addEventListener("keydown", capsLock);
  p.addEventListener("keyup", capsLock);
  p.addEventListener("blur", function () { caps.hidden = true; });
  if (u.value) p.focus();  // after a failed attempt the username is kept: only the password needs retyping
  f.addEventListener("submit", function () {
    reveal(false);  // never submit (or let the browser remember) the field as plain text
    go.disabled = true; go.textContent = "Signing in…";
  });
  addEventListener("pageshow", function (e) { if (e.persisted) { go.disabled = false; go.textContent = "Sign in"; } });
})();
</script>
</body>
</html>
"""
THEMES = ("graphite", "rose", "amethyst", "plum", "sage")  # all dark; the palettes are in index.html and LOGIN_HTML


def default_theme() -> str:
    """This ledger's theme for a browser that hasn't picked one: personal.json "dashboard.theme", else graphite."""
    try:
        t = personal.section("dashboard").get("theme")
    except Exception:  # the sign-in page must still open when personal.json is missing or broken
        return THEMES[0]
    return t if t in THEMES else THEMES[0]


def with_theme(page: str) -> str:
    """The page with the ledger's default theme on <html> (its own script then applies the viewer's saved choice)."""
    t = default_theme()
    return page if t == THEMES[0] else page.replace('<html lang="en">', f'<html lang="en" data-theme="{t}">', 1)


# One line of public market closes for the sign-in page (and the dashboard's top), read from the prices the daily sync already stores
# (insights/prices.py): nothing is fetched at request time. The page is public, so the lookup is memoized.
# The sign-in page is public, so its one line of market closes is limited to this fixed list of broad benchmarks
# below. Nothing else in the prices table, and nothing from the ledger, can ever reach that page:
# which other symbols are stored could hint at what is held.
_INDEX_ICON = ('<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="8" fill="%s"/><path d="M4 10.2l2.6-2.6 '
               '1.8 1.8L12 5.8" fill="none" stroke="#fff" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>')
_BTC_ICON = ('<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="8" fill="#f7931a"/><path d="M6.2 4.6h2.6a1.7 1.7 0 0 1 '
             '0 3.4H6.2zm0 3.4h3.1a1.7 1.7 0 0 1 0 3.4H6.2zM7.3 3.2v1.4M9 3.2v1.4M7.3 11.4v1.4M9 11.4v1.4" fill="none" stroke="#fff" '
             'stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>')
_GOLD_ICON = ('<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="8" fill="#d9a514"/><path d="M5.3 10.4l1.2-4.6h3l1.2 '
              '4.6z" fill="none" stroke="#fff" stroke-width="1.3" stroke-linejoin="round"/></svg>')
LOGIN_MARKETS = (("BTC", "Bitcoin", "$", _BTC_ICON), ("XAU", "Gold", "$", _GOLD_ICON),
                 ("SPX", "S&P 500", "", _INDEX_ICON % "#1f9d76"))  # symbol, label, value prefix, icon (shown in this order)
_INFL_ICON = ('<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="8" fill="#6f7fe8"/><g fill="none" stroke="#fff" '
              'stroke-width="1.3" stroke-linecap="round"><circle cx="5.9" cy="5.9" r="1.25"/><circle cx="10.1" cy="10.1" r="1.25"/>'
              '<path d="M10.6 5.4L5.4 10.6"/></g></svg>')
LOGIN_MARKETS_TTL = 600      # seconds a result is reused, an empty or failed one too
LOGIN_MARKETS_MAX_AGE = 10   # days: an older close is left out rather than shown as current
LOGIN_MARKETS_PREV_GAP = 7   # days: the change is against the close before, if that is at most this much earlier
_login_markets: tuple[float, str] | None = None  # (time.monotonic() when built, html); replaced whole
_login_markets_lock = threading.Lock()


def _market_closes_html() -> str:
    today = date.today()
    since = (today - timedelta(days=LOGIN_MARKETS_MAX_AGE + LOGIN_MARKETS_PREV_GAP)).isoformat()
    rows = q("SELECT symbol, date, usd FROM prices WHERE symbol IN (%s) AND date >= ? ORDER BY date DESC"
             % ",".join("?" * len(LOGIN_MARKETS)), [*(m[0] for m in LOGIN_MARKETS), since])

    def close(r):  # (day, usd), or None when the row isn't a date and a positive number
        try:
            day, usd = date.fromisoformat(str(r["date"])[:10]), float(r["usd"])
        except (TypeError, ValueError):
            return None
        return (day, usd) if 0 < usd < float("inf") else None
    parts, newest = [], None
    for sym, label, prefix, icon in LOGIN_MARKETS:
        mine = [close(r) for r in rows if r["symbol"] == sym][:2]  # the latest close and the one before it
        # -2: the sync stamps UTC dates, which can run a day ahead of local time; anything later is a bad row
        if not mine or not mine[0] or not (-2 <= (today - mine[0][0]).days <= LOGIN_MARKETS_MAX_AGE):
            continue
        (day, usd), prev, chg = mine[0], mine[1] if len(mine) > 1 else None, ""
        if prev and 0 < (day - prev[0]).days <= LOGIN_MARKETS_PREV_GAP:  # daily move: new ledgers have only a week stored
            pct = round(100 * (usd / prev[1] - 1), 1)
            chg = (f'<i class="{"up" if pct > 0 else "down" if pct < 0 else "flat"}" title="Since the close before, '
                   f'{prev[0].isoformat()}">{"+" if pct > 0 else "−" if pct < 0 else ""}{abs(pct):.1f}%</i>')
        parts.append(f'<div><span class="n">{icon}{html_escape(label)}</span><b>{prefix}{round(usd):,}</b>{chg}</div>')
        newest = max(newest or day, day)
    try:  # the inflation figure the sync keeps beside the prices (insights/prices.py: the Cleveland Fed's CPI nowcast)
        infl = json.loads(q("SELECT value FROM meta WHERE key = 'inflation'")[0]["value"])
        pct, asof = float(infl["pct"]), date.fromisoformat(str(infl["asof"])[:10])
        if -5 < pct < 30 and -2 <= (today - asof).days <= LOGIN_MARKETS_MAX_AGE:
            parts.append(f'<div><span class="n">{_INFL_ICON}Inflation</span><b>{pct:.1f}%</b><i title="Consumer prices, year '
                         f'over year: the Cleveland Fed nowcast as of {asof.isoformat()}">a year</i></div>')
            newest = max(newest or asof, asof)
    except (sqlite3.OperationalError, IndexError, KeyError, TypeError, ValueError):
        pass
    if not parts:
        return ""
    try:  # when the sync last fetched them; a ledger from before that was recorded has only the date
        when = datetime.fromisoformat(q("SELECT value FROM meta WHERE key = 'prices_updated'")[0]["value"]).astimezone()
        note = f"Updated {when:%b} {when.day}, {when.hour % 12 or 12}:{when:%M %p %Z}".rstrip()
    except (sqlite3.OperationalError, IndexError, TypeError, ValueError):
        note = f"As of {newest:%b} {newest.day}"
    return (f'<div class="mkt" title="Market closes as of {newest.isoformat()}">{"".join(parts)}</div>'
            f'<p class="upd">{html_escape(note)}</p>')


def login_markets() -> str:
    """'<div class="mkt" …><div>…S&amp;P 500…<b>6,812</b><i class="up">+0.4%</i>…</div>…</div><p class="upd">Updated …</p>'
    for the sign-in page: the latest stored close of each LOGIN_MARKETS symbol and its move since the close before,
    then the inflation nowcast, or "" when none is usable. Never raises and reads nothing but the prices table; at most one small query per
    LOGIN_MARKETS_TTL, whatever the traffic."""
    global _login_markets
    memo = _login_markets
    if memo and time.monotonic() - memo[0] < LOGIN_MARKETS_TTL:
        return memo[1]
    if not _login_markets_lock.acquire(blocking=False):  # another request is already looking it up
        return memo[1] if memo else ""
    try:
        try:
            html = _market_closes_html()
        except sqlite3.OperationalError:  # no prices table (a ledger that never ran the price step), no DB, locked
            html = ""
        except Exception as e:
            log(f"login markets: {e!r}")
            html = ""
        _login_markets = (time.monotonic(), html)
    finally:
        _login_markets_lock.release()
    return html


LOGIN_ERR_ICON = ('<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" '
                  'stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.01"/></svg>')


def script_hashes(page: bytes) -> str:
    """CSP sources for a page's own inline scripts: one 'sha256-…' per <script>…</script> block."""
    return " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(m).digest()).decode()
                    for m in re.findall(rb"<script>(.*?)</script>", page, re.S))


# the sign-in page's one inline script (show password, Caps Lock hint, busy button); the page works without it
CSP_LOGIN = f"{CSP_BASE}; script-src {script_hashes(LOGIN_HTML.encode())}; style-src 'unsafe-inline'; img-src data:"


def index_csp(page: bytes) -> str:
    return (f"{CSP_BASE}; script-src {script_hashes(page)} {CHARTJS}; style-src 'self' 'unsafe-inline'; "
            f"img-src 'self' data:; connect-src 'self'")


class Handler(BaseHTTPRequestHandler):
    server_version = "wealth-ledger-dashboard"
    sys_version = ""

    def version_string(self):
        return self.server_version
    timeout = 30  # drop slow/idle clients

    def log_message(self, fmt, *args):  # quiet; errors and auth events go to the journal via log()
        pass

    # --- helpers
    def _https(self) -> bool:
        h = getattr(self, "headers", None)
        if h is None:
            return False
        return (h.get("X-Forwarded-Proto", "").strip().lower() == "https"
                or "https" in h.get("Cf-Visitor", "").lower())

    def end_headers(self):  # security headers on every response, including stdlib error pages
        self.send_header("Cache-Control", getattr(self, "_cache", None) or "no-store")
        self._cache = None  # one response only; a later one on this connection must not inherit it
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        self.send_header("Content-Security-Policy", getattr(self, "_csp", None) or CSP_BASE)
        if self._https():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def _send(self, code: int, body: bytes, ctype: str, headers=(), csp: str | None = None, cache: str | None = None):
        self._csp, self._cache = csp, cache
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj, headers=()):
        return self._send(code, json.dumps(obj).encode(), "application/json", headers)

    def _redirect(self, to: str, headers=()):
        return self._send(303, b"", "text/plain", [("Location", to), *headers])

    def _client_ip(self) -> str:
        peer = self.client_address[0]
        cf = self.headers.get("Cf-Connecting-IP", "").strip()
        # trust the tunnel's header only from loopback (where cloudflared connects), never from the tailnet
        if cf and peer in ("127.0.0.1", "::1") and len(cf) <= 45 and re.fullmatch(r"[0-9A-Fa-f:.]+", cf):
            return cf
        return peer

    def _cookie_values(self) -> list[str]:
        out = []
        for h in self.headers.get_all("Cookie") or []:
            for part in h.split(";"):
                name, _, val = part.strip().partition("=")
                if name == COOKIE:
                    out.append(val.strip().strip('"'))
        return out[:5]

    def _authed(self) -> bool:
        return any(AUTH.check(v) for v in self._cookie_values())

    def _renewal(self) -> list[tuple[str, str]]:
        for v in self._cookie_values():
            if AUTH.check(v):
                fresh = AUTH.renew(v)
                return [self._set_cookie(fresh, SESSION_TTL)] if fresh else []
        return []

    def _set_cookie(self, value: str, max_age: int) -> tuple[str, str]:
        # Lax, not Strict: Strict drops the cookie when the dashboard is opened from a link in another app or site,
        # which looks like being logged out. Cross-site POSTs are still refused by _cross_site().
        c = f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax"
        return ("Set-Cookie", c + "; Secure" if self._https() else c)

    def _deny(self, path: str):
        html = path in ("/", "/index.html") or (
            not path.startswith("/api/") and path != "/healthz" and "text/html" in self.headers.get("Accept", ""))
        return self._redirect("/login") if html else self._json(401, {"error": "unauthorized"})

    def _cross_site(self) -> bool:
        # SameSite=Lax keeps the cookie off cross-site POSTs; also refuse them outright
        return self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none")

    def _read_form(self, limit: int = 4096) -> dict | None:
        n = self.headers.get("Content-Length", "0").strip()
        if not (n.isascii() and n.isdigit()) or int(n) > limit:
            return None
        body = self.rfile.read(int(n)).decode("utf-8", "replace") if int(n) else ""
        try:
            return parse_qs(body, max_num_fields=10)
        except ValueError:
            return None

    def _login_page(self, code: int = 200, error: str = "", headers=(), username: str = ""):
        # a failed attempt keeps the typed username (never the password), so only the password is retyped
        err = f'<p class="err" role="alert">{LOGIN_ERR_ICON}<span>{html_escape(error)}</span></p>' if error else ""
        # the page is public: besides the theme it shows only the allowlisted market closes, the error and the typed
        # username (replaced last, so typed text can't smuggle in a placeholder)
        page = (with_theme(LOGIN_HTML).replace("{{error}}", err).replace("{{markets}}", login_markets())
                .replace("{{username}}", html_escape(username)))
        return self._send(code, page.encode(), "text/html; charset=utf-8", headers, CSP_LOGIN)

    # --- routes
    def do_GET(self):
        p = "?"
        try:
            u = urlparse(self.path)
            p = u.path
            if p == "/robots.txt":
                return self._send(200, ROBOTS, "text/plain; charset=utf-8")
            if p == "/login":
                return self._redirect("/") if self._authed() else self._login_page()
            if p == "/logout":
                return self._send(405, b"use POST\n", "text/plain", [("Allow", "POST")])
            if not self._authed():
                return self._deny(p)
            if p in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
                    page = with_theme(f.read()).replace("{{markets}}", login_markets(), 1).encode()
                return self._send(200, page, "text/html; charset=utf-8", self._renewal(), index_csp(page))
            if p == "/healthz":
                return self._send(200, b"ok\n", "text/plain")
            if m := ICON_PATH.fullmatch(p):
                icon = icon_bytes(m.group(1))
                return self._send(200, icon, "image/png", cache="private, max-age=86400") if icon else self._json(404, {"error": "not found"})
            if p == "/api/last":
                return self._send(200, last_good(), "application/json")
            fn = ROUTES.get(p)
            if not fn:
                return self._json(404, {"error": "not found"})
            params = parse_qs(u.query)
            params.pop("refresh", None)  # refresh is POST /api/refresh only
            res = fn(params)
            body = json.dumps(res).encode()
            if not params:
                remember(p.removeprefix("/api/"), res, body)
            return self._send(200, body, "application/json")
        except Exception as e:
            log(f"error on GET {p}: {e!r}")
            return self._json(500, {"error": str(e) if self._authed() else "internal error"})

    def do_POST(self):
        p = "?"
        try:
            p = urlparse(self.path).path
            if self._cross_site():
                log(f"auth: refused cross-site POST {p} ip={self._client_ip()}")
                return self._json(403, {"error": "cross-site request refused"})
            form = self._read_form()
            if form is None:
                return self._json(413, {"error": "bad or oversized request body"})
            if p == "/login":
                user = form.get("username", [""])[0][:256]
                pw = form.get("password", [""])[0][:1024]
                status, extra = AUTH.login(self._client_ip(), user, pw)
                if status == "ok":
                    return self._redirect("/", [self._set_cookie(extra, SESSION_TTL)])
                if status == "locked":
                    mins = max(1, round(extra / 60))
                    return self._login_page(429, f"Too many failed attempts. Try again in about {mins} min.",
                                            [("Retry-After", str(extra))], username=user)
                return self._login_page(401, "Wrong username or password.", username=user)
            if p == "/logout":
                AUTH.revoke(self._cookie_values())
                return self._redirect("/login", [self._set_cookie("", 0)])
            if not self._authed():
                return self._deny(p)
            if p == "/api/connect":
                code, body = start_connect(form.get("token", [""])[0][:2048])
                return self._json(code, body)
            if p == "/api/refresh":  # re-reads the balance sheet via networth.py; POST so it can't be triggered cross-site
                res = api_summary({"refresh": ["1"]})
                body = json.dumps(res).encode()
                remember("summary", res, body)
                return self._send(200, body, "application/json")
            return self._json(404, {"error": "not found"})
        except Exception as e:
            log(f"error on POST {p}: {e!r}")
            return self._json(500, {"error": "internal error"})


def tailscale_ip() -> str | None:
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10).stdout.split()
        return out[0] if out else None
    except Exception:
        return None


def serve(host: str, port: int):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"listening on http://{host}:{port}/", flush=True)
    httpd.serve_forever()


def tailnet_binder(port: int):
    """Bind the Tailscale IP once tailscaled has one; retry every 30 s so boot order doesn't matter."""
    while True:
        ip = tailscale_ip()
        if ip:
            try:
                serve(ip, port)
            except OSError as e:
                print(f"bind {ip}:{port} failed: {e}; retrying", file=sys.stderr, flush=True)
        time.sleep(30)


def main():
    if sys.argv[1:2] == ["set-password"]:
        return cli_set_password(sys.argv[2:])
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", 8766)))
    ap.add_argument("--no-tailscale", action="store_true")
    a = ap.parse_args()
    if not a.no_tailscale:
        threading.Thread(target=tailnet_binder, args=(a.port,), daemon=True).start()
    if AUTH.creds():
        log(f"auth: credentials loaded from {AUTH_FILE}")
    threading.Thread(target=networth, daemon=True).start()  # warm the cache
    serve("127.0.0.1", a.port)


if __name__ == "__main__":
    main()
