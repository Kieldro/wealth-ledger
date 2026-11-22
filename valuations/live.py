"""Market-priced assets at prices fetched now, for the dashboard and `networth.py --live`: the quantities the daily
runs stored, times live prices. Quantities never change here, and nothing is written anywhere (not the ledger, not
the sheet).

  crypto  each wallet's coins from the latest valuations/crypto.py run x crypto.prices() (CoinGecko, then Kraken,
          then Coinbase per coin), without its Kraken cross-check, so one CoinGecko request prices every coin
  metals  troy oz per metal from the latest valuations/metals.py run x metals.spot_price() (Swissquote, then
          gold-api.com, then Yahoo futures)
  stocks  shares in the latest synced holdings x Yahoo Finance's keyless spark quotes (the last trade, 15-minute
          delayed at worst; up to 20 symbols a request): stocks, ETFs (FBTC and the other bitcoin funds too), ADRs,
          OTC shares. Mutual funds (five-letter tickers ending in X) keep their last NAV, and money market / cash
          sweeps $1 a share, as in insights/holding_prices.py. A coin held at a synced crypto exchange is priced as
          crypto, never as the ticker of the same name (BTC is also a fund).

A synced account with holdings is valued the way the trend values each day (holding_prices.revalue), with the live
quote as today's close, so the headline, the trend's last point and its 1-day change agree.

Each group is fetched at most once a TTL per process (a failed fetch too, so a down source isn't hammered), the three
at once. A request waits at most WAIT seconds; a slower group lands in the cache for the next one. A price is used
while it is newer than the stored daily run and at most MAX_AGE old; otherwise the stored valuation stands. Problems
are only returned ("error"), never notified.

WEALTH_LEDGER_LIVE_PRICES: unset = the sources above; "off" = no live prices; a path = read prices from that JSON
file ({"crypto": {"BTC": usd}, "metals": {"Au": usd}, "stocks": {"VTI": usd}}) on every request instead of the
network (the browser tests' dashboard; no file = off, a group missing from it = that source down).
"""
from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait as wait_for
from datetime import date, datetime
from zoneinfo import ZoneInfo

from insights import holding_prices
from valuations import crypto, metals

ENV = "WEALTH_LEDGER_LIVE_PRICES"
TTL = 60              # seconds a fetch (or a failed one) is reused
WAIT = 6.0            # seconds a request waits for a fetch under way
MAX_AGE = 30 * 60     # seconds: an older live price is dropped and the stored daily valuation stands
GROUPS = ("crypto", "metals", "stocks")
CRYPTO_LINE, METALS_LINE = crypto.ASSETS[0], metals.ASSETS[0]
SPARK = "https://query1.finance.yahoo.com/v8/finance/spark"
SPARK_MAX = 20        # symbols per spark request (Yahoo refuses more)
MARKET_TZ = ZoneInfo("America/New_York")  # a quote's trading day
TICKER = re.compile(r"[A-Z][A-Z0-9]{0,5}(?:[.-][A-Z]{1,2})?")
EL = {name: el for el, name in metals.METALS.items()}  # "gold" -> "Au"
UA = {"User-Agent": "Mozilla/5.0 (wealth-ledger live prices)"}


def enabled() -> bool:
    v = os.environ.get(ENV, "").strip()
    return v.lower() not in ("off", "0", "false", "no") and (not v or os.path.exists(v))


def is_mutual_fund(sym: str) -> bool:
    return len(sym) == 5 and sym.isalpha() and sym.endswith("X")


def quotable(sym: str) -> bool:
    """A symbol with a live market price on Yahoo: not cash, not a mutual fund (priced once a day), not a made-up id."""
    sym = (sym or "").strip()
    return bool(TICKER.fullmatch(sym)) and not holding_prices.is_cash(sym) and not is_mutual_fund(sym)


# ---------------------------------------------------------------- what is held (stored quantities)

def latest_valuation(con: sqlite3.Connection, asset: str) -> dict | None:
    """{date, value, detail, fetched_at (epoch)} of the asset's latest valuations row, or None."""
    try:
        r = con.execute("SELECT snapshot_date, value, detail, fetched_at FROM v_valuations_latest WHERE asset = ?",
                        (asset,)).fetchone()
    except sqlite3.OperationalError:  # DB predates the valuations table
        return None
    if not r or not r[2]:
        return None
    try:
        fetched = datetime.fromisoformat(r[3]).timestamp()
    except (TypeError, ValueError):
        fetched = 0.0
    return {"date": r[0], "value": r[1], "detail": json.loads(r[2]), "fetched_at": fetched}


def positions(con: sqlite3.Connection) -> dict[str, dict]:
    """{account_id: {balance, positions: [(symbol, shares, price, market value)]}} of every counted account whose
    latest balance snapshot has holdings."""
    kinds = holding_prices.COUNTED_KINDS
    try:
        rows = con.execute(f"""SELECT a.account_id, CAST(a.balance AS REAL), TRIM(COALESCE(h.symbol, '')),
                                      CAST(NULLIF(TRIM(h.shares), '') AS REAL), CAST(NULLIF(TRIM(h.price), '') AS REAL),
                                      CAST(NULLIF(TRIM(h.market_value), '') AS REAL)
                               FROM v_accounts a JOIN holdings h ON h.account_id = a.account_id
                                 AND h.snapshot_date = a.snapshot_date AND h.source = a.source
                               WHERE a.kind IN ({','.join('?' * len(kinds))})
                                 AND a.account_id NOT IN (SELECT account_id FROM superseded)""", kinds).fetchall()
    except sqlite3.OperationalError:  # older DB without holdings
        return {}
    out: dict[str, dict] = {}
    for aid, bal, sym, shares, price, mv in rows:
        out.setdefault(aid, {"balance": bal or 0.0, "positions": []})["positions"].append((sym, shares, price, mv))
    return out


def _shown_holdings(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """(symbol, account kind) of every synced holding the dashboard lists."""
    try:
        return con.execute("""SELECT DISTINCT TRIM(h.symbol), COALESCE(k.kind, '') FROM v_holdings_latest h
                              LEFT JOIN account_kind k USING (account_id)
                              WHERE TRIM(COALESCE(h.symbol, '')) <> '' AND h.source <> 'manual'
                                AND COALESCE(k.kind, '') <> 'custodial'""").fetchall()
    except sqlite3.OperationalError:
        return []


def wanted(con: sqlite3.Connection) -> dict[str, set[str]]:
    """What to price: {"crypto": coins, "metals": elements (Au), "stocks": feed symbols}."""
    want = {g: set() for g in GROUPS}
    v = latest_valuation(con, CRYPTO_LINE)
    for h in (v["detail"].get("holdings") or []) if v else []:
        if h.get("coin") and h["coin"] != "USD" and h.get("qty"):
            want["crypto"].add(h["coin"])
    v = latest_valuation(con, METALS_LINE)
    for name, m in ((v["detail"].get("metals") or {}).items() if v else ()):
        if name in EL and m.get("oz"):
            want["metals"].add(EL[name])
    for p in positions(con).values():
        want["stocks"].update(s for s, *_ in p["positions"] if quotable(s))
    for sym, kind in _shown_holdings(con):
        if kind == "crypto":
            coin = crypto.norm_coin(sym)
            if coin != "USD":
                want["crypto"].add(coin)
        elif quotable(sym):
            want["stocks"].add(sym)
    return want


# ---------------------------------------------------------------- fetching: each -> {key: (usd, source, quote time)}

def fetch_crypto(coins: list[str]) -> dict[str, tuple]:
    px, _ = crypto.prices(set(coins), {}, check=False)
    return {c: (p, src, None) for c, (p, src) in px.items() if c != "USD"}


def fetch_metals(els: list[str]) -> dict[str, tuple]:
    out = {}
    for el in els:
        try:
            p, src = metals.spot_price(el)
        except RuntimeError:  # every source failed for this metal: its stored spot stands
            continue
        out[el] = (p, src, None)
    return out


def _get(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as f:
        return json.load(f)


def fetch_stocks(symbols: list[str]) -> dict[str, tuple]:
    """Yahoo's spark chart (5-minute bars of the current or last session): the last bar's close is the last trade."""
    ysym = {holding_prices.yahoo_symbol(s): s for s in symbols}
    names, out = sorted(ysym), {}
    for i in range(0, len(names), SPARK_MAX):
        qs = urllib.parse.urlencode({"symbols": ",".join(names[i:i + SPARK_MAX]), "range": "1d", "interval": "5m"})
        d = _get(f"{SPARK}?{qs}")
        for name, v in (d.items() if isinstance(d, dict) else ()):
            if name not in ysym or not isinstance(v, dict):
                continue
            bars = [(t, c) for t, c in zip(v.get("timestamp") or [], v.get("close") or []) if c is not None and c > 0]
            if bars:
                out[ysym[name]] = (float(bars[-1][1]), "yahoo", float(bars[-1][0]))
    return out


FETCHERS = {"crypto": fetch_crypto, "metals": fetch_metals, "stocks": fetch_stocks}

# ---------------------------------------------------------------- cache

_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=len(GROUPS), thread_name_prefix="live-prices")
_state: dict[str, dict] = {}


def reset() -> None:
    with _lock:
        _state.clear()
        _state.update({g: {"prices": {}, "asked": set(), "tried": 0.0, "error": None, "future": None} for g in GROUPS})


reset()


def _fetch(group: str, keys: frozenset) -> None:
    """Runs in the pool: fetch one group and store what came back, before the future completes."""
    stub = os.environ.get(ENV, "").strip()
    try:
        if stub:  # canned prices instead of the network
            with open(stub) as f:
                table = json.load(f).get(group) or {}
            got = {k: (float(table[k]), "stub", None) for k in keys if k in table}
        else:
            got = FETCHERS[group](sorted(keys))
        error = None if got else f"{group}: no price from any source"
    except Exception as e:  # noqa: BLE001 - the stored daily prices stand
        got, error = {}, f"{group}: {e}"
    now = time.time()
    with _lock:
        g = _state[group]
        for k, (usd, src, t) in got.items():
            g["prices"][k] = {"usd": usd, "source": src, "time": t, "at": now}
        g["error"], g["future"] = error, None


def quotes(want: dict[str, set[str]], wait: float = WAIT) -> dict:
    """Live prices for `want` (see wanted()): {group: {"prices": {key: {usd, source, time, at}}, "error"}}, at most
    MAX_AGE old. Starts a fetch for each group not fetched (or tried) in the last TTL, or asked for something new,
    and waits up to `wait` seconds for them. Empty when live prices are off."""
    if not enabled():
        return {g: {"prices": {}, "error": None} for g in GROUPS}
    ttl = 0 if os.environ.get(ENV, "").strip() else TTL
    futs = []
    with _lock:
        for group in GROUPS:
            keys, g = frozenset(want.get(group) or ()), _state[group]
            if keys and g["future"] is None and (time.time() - g["tried"] >= ttl or not keys <= g["asked"]):
                g["asked"], g["tried"] = set(keys), time.time()
                g["future"] = _pool.submit(_fetch, group, keys)
            if g["future"] is not None:
                futs.append(g["future"])
    if futs:
        wait_for(futs, timeout=wait)
    now = time.time()
    with _lock:
        return {group: {"prices": {k: dict(r) for k, r in g["prices"].items() if now - r["at"] <= MAX_AGE},
                        "error": g["error"]} for group, g in _state.items()}


# ---------------------------------------------------------------- repricing

def _live(q: dict, group: str, key: str, after: float = 0.0) -> dict | None:
    r = (q.get(group) or {}).get("prices", {}).get(key)
    return r if r and r["at"] > after else None


def when(rs: list[dict]) -> dict:
    """When a group's prices are from: the oldest fetch among them, the newest market quote time, the sources."""
    times = [r["time"] for r in rs if r.get("time")]
    return {"at": min(r["at"] for r in rs), "quote_time": max(times) if times else None,
            "sources": sorted({r["source"] for r in rs})}


def reprice_crypto(val: dict | None, q: dict) -> dict | None:
    """The latest valuations/crypto.py run with live prices: {total, holdings, by_coin, when}; None when no live price
    applies (each coin keeps its stored price unless a newer live one exists)."""
    if not val:
        return None
    holdings, used = [], []
    for h in val["detail"].get("holdings") or []:
        r = _live(q, "crypto", h.get("coin"), val["fetched_at"]) if h.get("coin") != "USD" else None
        if r and h.get("qty") is not None:
            used.append(r)
            h = {**h, "price": r["usd"], "price_source": r["source"], "value": round(h["qty"] * r["usd"], 2)}
        holdings.append(h)
    if not used:
        return None
    by_coin: dict[str, dict] = {}
    for h in holdings:  # as crypto.combine adds them up
        if h.get("counted", True):
            b = by_coin.setdefault(h["coin"], {"qty": 0.0, "price": h.get("price"), "value": 0.0})
            b["qty"] = round(b["qty"] + (h.get("qty") or 0), 8)
            b["value"] = round(b["value"] + (h.get("value") or 0), 2)
    total = round(sum(h.get("value") or 0 for h in holdings if h.get("counted", True)), 2)
    return {"total": total, "holdings": holdings, "when": when(used),
            "by_coin": dict(sorted(by_coin.items(), key=lambda kv: -kv[1]["value"]))}


def reprice_metals(val: dict | None, q: dict) -> dict | None:
    """The latest valuations/metals.py run at live spot: {total, metals, when}; None when no live spot applies."""
    if not val:
        return None
    out, used, total = {}, [], 0.0
    for name, m in (val["detail"].get("metals") or {}).items():
        r = _live(q, "metals", EL.get(name), val["fetched_at"])
        value = m.get("value")
        if value is None and m.get("spot") is not None:
            value = (m.get("oz") or 0) * m["spot"]
        if r:
            used.append(r)
            m = {**m, "spot": round(r["usd"], 2), "source": r["source"]}
            value = (m.get("oz") or 0) * r["usd"]
        out[name] = {**m, "value": round(value or 0.0, 2)}
        total += value or 0.0
    return {"total": round(total, 2), "metals": out, "when": when(used)} if used else None


def stock_closes(closes: dict, q: dict, today: str) -> dict:
    """holding_prices.load() closes with each live quote as the close of its trading day (replacing a stored close of
    that day or later), so revalue() on `today` uses it. Only symbols with a live quote change."""
    out = dict(closes)
    for sym, r in ((q.get("stocks") or {}).get("prices") or {}).items():
        day = datetime.fromtimestamp(r["time"], MARKET_TZ).date().isoformat() if r.get("time") else today
        day = min(day, today)
        out[sym] = [c for c in closes.get(sym, []) if c[0] < day] + [(day, r["usd"])]
    return out


def account_values(con: sqlite3.Connection, q: dict, today: str | None = None) -> dict[str, tuple[float, float]]:
    """{account_id: (live value, stored balance)} of every counted account with holdings, valued at today's close with
    the live quotes as today's (revalue: cash $1 a share, a mutual fund at its last NAV, anything unpriced or
    implausible at the feed's own value). {} when there are no live stock quotes."""
    if not (q.get("stocks") or {}).get("prices"):
        return {}
    today = today or date.today().isoformat()
    pos = positions(con)
    closes = stock_closes(holding_prices.load(con, {s for p in pos.values() for s, *_ in p["positions"]}), q, today)
    return {aid: (holding_prices.revalue(p["balance"], p["positions"], closes, today)[0], p["balance"])
            for aid, p in pos.items()}


def holding_quote(q: dict, symbol: str, kind: str, feed_price: float | None) -> tuple[str, dict] | None:
    """(group, live price record) for one synced holding, or None to keep the feed's value: a coin at a crypto
    exchange at the coin's price, a quotable ticker at its last trade; only within holding_prices.SANE of the feed's
    own price (else it is a different security, or a split). A coin needs no feed price (exchanges send none)."""
    sym = (symbol or "").strip()
    group, key = ("crypto", crypto.norm_coin(sym)) if kind == "crypto" else ("stocks", sym if quotable(sym) else None)
    r = _live(q, group, key) if key else None
    if not r or (not feed_price and group == "stocks"):
        return None
    if feed_price and not holding_prices.SANE[0] <= r["usd"] / feed_price <= holding_prices.SANE[1]:
        return None
    return group, r


def reprice(result: dict, con: sqlite3.Connection, q: dict, today: str | None = None) -> dict:
    """networth.compute()'s result at live prices: the crypto and metals lines from the live totals, each synced asset
    line by its accounts' live values; totals recomputed. A changed line keeps its daily number as "stored".
    Adds "prices": {group: {at, quote_time, sources} for each group priced live}."""
    out = copy.deepcopy(result)
    vals = {asset: latest_valuation(con, asset) for asset in (CRYPTO_LINE, METALS_LINE)}
    reps = {CRYPTO_LINE: ("crypto", reprice_crypto(vals[CRYPTO_LINE], q)),
            METALS_LINE: ("metals", reprice_metals(vals[METALS_LINE], q))}
    accts = account_values(con, q, today)
    used_stocks = list((q.get("stocks") or {}).get("prices", {}).values())
    priced = {}
    for line in out["lines"]:
        new = None
        group, rep = reps.get(line["line"], (None, None))
        val = vals.get(line["line"])
        # only a line that is showing the stored valuation (not a synced balance, not left out by the sheet)
        if rep and val and not line.get("synced") and line["from"].startswith(("live reading", "estimate")) \
                and abs((line["used"] or 0) - val["value"]) < 0.01:
            new, priced[group] = rep["total"], rep["when"]
        ids = [a for a in line.get("accounts") or [] if a in accts]
        if ids and line["type"] == "asset":
            new = (line["used"] or 0) + sum(accts[a][0] - accts[a][1] for a in ids)
            priced["stocks"] = when(used_stocks)
        if new is not None and abs(new - (line["used"] or 0)) >= 0.005:
            line["stored"], line["used"] = line["used"], round(new, 2)
    assets = sum(l["used"] for l in out["lines"] if l["type"] == "asset")
    liabs = sum(l["used"] for l in out["lines"] if l["type"] == "liability")
    out.update(assets=round(assets, 2), liabilities=round(liabs, 2), net_worth=round(assets - liabs, 2), prices=priced)
    return out
