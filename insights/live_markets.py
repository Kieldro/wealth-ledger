"""Live prices for the markets line on the sign-in page and the dashboard: Bitcoin, gold and the S&P 500, fetched now
(the line's other items keep what insights/prices.py stores). Nothing else is priced live: net worth, holdings and
the trend stay at the daily run's prices.

Free, keyless sources:
  BTC  Coinbase's spot price, falling back to CoinGecko
  XAU  valuations/metals.spot_price("Au"): Swissquote's XAU/USD bid/ask mid, then gold-api.com, then Yahoo futures
  SPX  Yahoo Finance's ^GSPC last trade (moves only while the market is open; after the close, the close)

Each quote carries its trading day ("day"), so the strip can measure the move from the stored close before that day:
  BTC  the UTC day (the stored closes are Coinbase's UTC-day candles)
  XAU  the COMEX trade date: a session opens at 6 PM New York time for the next day; Saturday and Sunday are Friday's
  SPX  the New York date of the last trade, so a weekend still shows Friday's move against Thursday's close

Each symbol is fetched at most once a TTL per process (a failed try too, so a down source isn't hammered), the three
at once in background threads. A caller waits at most WAIT seconds; a slower answer lands for the next call. A quote
is used for MAX_AGE after it was fetched, then dropped (the strip then shows the stored close). Problems are logged
to stderr when a symbol starts failing, never notified, and nothing is written anywhere.

WEALTH_LEDGER_LIVE_MARKETS: unset = the sources above; "off" = no live prices; a path = read {"BTC": usd, "XAU": usd,
"SPX": usd} from that JSON file on every call instead of the network (the browser tests' dashboard; no file = off, a
symbol missing from it = that source down).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait as wait_for
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from insights.prices import PLAUSIBLE  # noqa: E402
from valuations import metals  # noqa: E402

ENV = "WEALTH_LEDGER_LIVE_MARKETS"
SYMBOLS = ("BTC", "XAU", "SPX")
TTL = 60          # seconds a fetch (or a failed one) is reused
WAIT = 5.0        # seconds a caller waits for a fetch under way
MAX_AGE = 5 * 60  # seconds a quote is used after it was fetched
TIMEOUT = 5       # seconds per HTTP request
NY = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (wealth-ledger markets)"}


def _get(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=TIMEOUT) as f:
        return json.load(f)


# ---------------------------------------------------------------- trading days

def btc_day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).date().isoformat()


def gold_day(t: float) -> str:
    d = (datetime.fromtimestamp(t, NY) + timedelta(hours=6)).date()  # 6 PM opens the next day's session
    return (d - timedelta(days=max(0, d.weekday() - 4))).isoformat()  # Saturday, Sunday -> Friday


def spx_day(t: float) -> str:
    return datetime.fromtimestamp(t, NY).date().isoformat()


# ---------------------------------------------------------------- sources
# each -> (usd shown, quote time, source, vs): vs is the price on the instrument the stored closes are of, the one the
# daily move is measured with (None: no move shown rather than one between two instruments)

def _yahoo_last(symbol: str) -> tuple[float, float]:
    """Yahoo's last trade of a symbol (during the session, else the last close) and its time."""
    meta = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1d&range=1d"
                )["chart"]["result"][0]["meta"]
    return float(meta["regularMarketPrice"]), float(meta["regularMarketTime"])


def fetch_btc() -> tuple[float, float, str, float | None]:
    try:
        usd = float(_get("https://api.coinbase.com/v2/prices/BTC-USD/spot")["data"]["amount"])
        return usd, time.time(), "coinbase", usd
    except Exception as e:  # noqa: BLE001 - try the next source
        first = e
    try:
        usd = float(_get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")["bitcoin"]["usd"])
        return usd, time.time(), "coingecko", usd
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"coinbase: {first}; coingecko: {e}") from None


def fetch_gold() -> tuple[float, float, str, float | None]:
    """Spot gold, and the COMEX front-month future the stored closes are of (insights/prices.py: GC=F) for the move:
    spot and the future differ by a fraction of a percent, too much to measure a day's move across them."""
    usd, src = metals.spot_price("Au")  # Swissquote, then gold-api.com, then Yahoo futures
    t = time.time()
    if src == "yahoo futures":
        return usd, t, src, usd
    try:
        return usd, t, src, _yahoo_last(metals.YAHOO["Au"])[0]
    except Exception:  # noqa: BLE001 - the price still shows, without a move
        return usd, t, src, None


def fetch_spx() -> tuple[float, float, str, float | None]:
    usd, t = _yahoo_last("^GSPC")
    return usd, t, "yahoo ^GSPC", usd


SOURCES = {"BTC": (fetch_btc, btc_day), "XAU": (fetch_gold, gold_day), "SPX": (fetch_spx, spx_day)}


# ---------------------------------------------------------------- cache

_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=len(SYMBOLS), thread_name_prefix="markets")
_state: dict[str, dict] = {}  # symbol -> {tried (monotonic), future, quote, error}


def reset() -> None:
    """Forget every fetch (tests)."""
    with _lock:
        _state.clear()


def _fetch(sym: str, st: dict) -> None:
    fetch, day = SOURCES[sym]
    try:
        usd, t, src, vs = fetch()
        lo, hi = PLAUSIBLE[sym]
        if not lo <= usd <= hi:
            raise ValueError(f"implausible {usd}")
        vs = vs if vs is not None and lo <= vs <= hi else None
    except Exception as e:  # noqa: BLE001 - the stored close stands
        if not st.get("error"):
            print(f"live markets: {sym}: {e!r}", file=sys.stderr, flush=True)
        st["error"] = repr(e)
        return
    st.update(quote={"usd": usd, "vs": vs, "day": day(t), "time": t, "at": time.time(), "source": src}, error=None)


def _from_file(path: str) -> dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            px = json.load(f)
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {s: {"usd": float(px[s]), "vs": float(px[s]), "day": SOURCES[s][1](now), "time": now, "at": now, "source": "file"}
            for s in SYMBOLS if isinstance(px.get(s), (int, float))}


def enabled() -> bool:
    v = os.environ.get(ENV, "").strip()
    return v.lower() not in ("off", "0", "false", "no") and (not v or os.path.exists(v))


def quotes(wait: float = WAIT) -> dict[str, dict]:
    """{symbol: {usd, vs, day, time (of the quote, epoch), at (fetched, epoch), source}} for each symbol with a usable live
    price; a symbol whose source is down, slow or off is left out. Starts the fetches that are due and waits for them
    at most `wait` seconds (0: answer from the cache at once)."""
    if not enabled():
        return {}
    if v := os.environ.get(ENV, "").strip():
        return _from_file(v)
    now = time.monotonic()
    with _lock:
        for sym in SYMBOLS:
            st = _state.setdefault(sym, {"tried": None, "future": None, "quote": None, "error": None})
            if (st["future"] is None or st["future"].done()) and (st["tried"] is None or now - st["tried"] >= TTL):
                st["tried"], st["future"] = now, _pool.submit(_fetch, sym, st)
        pending = [st["future"] for st in _state.values() if st["future"] and not st["future"].done()]
    if pending and wait > 0:
        wait_for(pending, timeout=wait)
    return {sym: st["quote"] for sym, st in list(_state.items())
            if st["quote"] and time.time() - st["quote"]["at"] <= MAX_AGE}
