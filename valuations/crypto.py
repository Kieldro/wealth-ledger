"""Crypto at live prices: per-wallet coin quantities from the best source available x keyless spot prices.

Holdings start from the sheet's `crypto` tab: blocks of "<COIN> price" (or "<COIN>" then "price") followed
by one row per wallet (A = wallet, B = quantity) and "Total ..." rows. The tab's grand total
"Total cryptocurrency (USD)" is a hand-picked SUM of some coins' "Total ... value" cells (it may leave
some coins out); that formula is parsed so the sheet's own number can be reproduced exactly.

Quantity per (wallet, coin), best source first:
  1. onchain       addresses / xpubs in ~/.config/wealth-ledger/crypto_wallets.json (+ xpubs found in the
                   *.txt wallet exports in personal.json crypto.xpub_export_dir): BTC via mempool.space
                   (blockstream.info fallback),
                   ETH + ERC-20 USDT/USDC via public RPCs, TRX + TRC-20 USDT via TronGrid, SOL via Solana RPC
  2. exchange-api  Gemini / Kraken private balance calls, only if read-only keys are in that file
  3. simplefin     exchange wallets synced into txns.db (kind='crypto'); networth.py counts them nowhere else
  4. sheet         the crypto tab's manual quantity
Prices: CoinGecko simple/price, falling back per coin to Kraken then Coinbase public tickers, then the
sheet's own price. Kraken is also used to cross-check CoinGecko.

Counted in the total: every coin in a self-custody / synced wallet, plus at the exchanges listed in
personal.json crypto.exchange_rows only the coins the sheet's total counts (the balance sheet has separate rows
for those exchanges that hold the rest of their balances, so counting them here would double count).

Never writes the sheet, never prints keys; an xpub is only sent to the BTC balance API.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import personal  # noqa: E402

NAME = "crypto"
EVERY_DAYS = 1
ASSETS = ("Total cryptocurrency (USD)",)

DB = Path.home() / ".local/share/wealth-ledger/txns.db"
CONFIG = Path.home() / ".config/wealth-ledger/crypto_wallets.json"
# set these to override personal.json (tests do); None = read personal.json "crypto" when first needed
XPUB_EXPORT_DIR: Path | None = None   # folder of wallet-export .txt files to scan for xpub/ypub/zpub keys
UA = {"User-Agent": "Mozilla/5.0 (wealth-ledger valuations)"}

# wallets with their own balance-sheet row: only the coins the sheet's crypto total counts are counted here
EXCHANGE_ROWS: set[str] | None = None
SOURCE_RANK = ("onchain", "exchange-api", "simplefin")  # best first; "sheet" is the fallback

# coin -> (CoinGecko id, Kraken ticker pair, result key)
COINS = {
    "BTC": ("bitcoin", "XBTUSD", "XXBTZUSD"), "ETH": ("ethereum", "ETHUSD", "XETHZUSD"),
    "XMR": ("monero", "XMRUSD", "XXMRZUSD"), "LTC": ("litecoin", "LTCUSD", "XLTCZUSD"),
    "TRX": ("tron", "TRXUSD", "TRXUSD"), "SOL": ("solana", "SOLUSD", "SOLUSD"),
    "BCH": ("bitcoin-cash", "BCHUSD", "BCHUSD"), "ALGO": ("algorand", "ALGOUSD", "ALGOUSD"),
    "DOGE": ("dogecoin", "XDGUSD", "XDGUSD"), "USDT": ("tether", "USDTZUSD", "USDTZUSD"),
    "USDC": ("usd-coin", "USDCUSD", "USDCUSD"), "ZRX": ("0x", "ZRXUSD", "ZRXUSD"),
}
ALIASES = {"XBT": "BTC", "XDG": "DOGE", "ETH2": "ETH", "GUSD": "USD", "ZUSD": "USD"}


def norm_coin(sym: str) -> str:
    """Exchange / feed asset code -> our coin symbol (Kraken XXBT, XBT.M, ETH2.S; Gemini btc; Coinbase ETH2)."""
    s = str(sym).upper().split(".")[0]
    if s in ALIASES:
        return ALIASES[s]
    if len(s) == 4 and s[0] in "XZ" and (s[1:] in COINS or s[1:] in ALIASES or s[1:] == "USD"):
        s = s[1:]
    return ALIASES.get(s, s)


# ---------------------------------------------------------------- HTTP

def _req(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 20):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return json.load(f)


def _rpc(urls: list[str], method: str, params: list):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    errors = []
    for url in urls:
        try:
            r = _req(url, body, {"Content-Type": "application/json"})
            res = r.get("result")
            # some gateways put their own error object in "result"; real results are hex strings or {value}
            if "error" not in r and res is not None and (not isinstance(res, dict) or "value" in res):
                return res
            errors.append(f"{urllib.parse.urlparse(url).hostname}: {r.get('error') or r.get('result')}")
        except Exception as e:  # noqa: BLE001 - next endpoint
            errors.append(f"{urllib.parse.urlparse(url).hostname}: {e}")
    raise RuntimeError(f"{method} failed: " + "; ".join(errors))


# ---------------------------------------------------------------- sheet

def _sheet(rng: str, render: str = "UNFORMATTED_VALUE") -> list[list]:
    out = subprocess.run(
        ["gws", "sheets", "spreadsheets", "values", "get", "--params",
         json.dumps({"spreadsheetId": personal.sheet_id(), "range": rng, "valueRenderOption": render})],
        capture_output=True, text=True, check=True, timeout=60).stdout
    return json.loads(out).get("values", [])


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def wallet_name(label: str) -> str:
    """'Hot wallet' -> 'Hot', 'Cold wallet (old)' -> 'Cold', 'exchange pro' -> 'Exchange'."""
    return str(label).strip().split()[0].capitalize() if str(label).strip() else ""


def parse_sheet(values: list[list], formulas: list[list]) -> dict:
    """crypto tab rows (UNFORMATTED + FORMULA renders) ->
    {coins: {SYM: {price, wallets{wallet: qty}, labels{wallet: label}, value_row}}, total, counted[SYM]}."""
    coins: dict[str, dict] = {}
    cur = None
    total, total_row = None, None
    for i, r in enumerate(values, start=1):
        r = list(r) + ["", ""]
        label, b = str(r[0]).strip(), _num(r[1])
        low = label.lower()
        if not label:
            continue
        if low.startswith("total cryptocurrency"):
            total, total_row = b, i
            continue
        m = re.fullmatch(r"([A-Za-z0-9]+) price", label)
        if m or (re.fullmatch(r"[A-Z0-9]{2,6}", label) and b is None):
            cur = norm_coin(m.group(1) if m else label)
            coins.setdefault(cur, {"price": b, "wallets": {}, "labels": {}, "value_row": None})
            continue
        if cur is None:
            continue
        if low == "price":
            coins[cur]["price"] = b
        elif low.startswith("total"):
            if "value" in low:
                coins[cur]["value_row"] = i
        elif b is not None:
            w = wallet_name(label)
            coins[cur]["wallets"][w] = coins[cur]["wallets"].get(w, 0.0) + b
            coins[cur]["labels"][w] = label
    if total_row is None:
        raise RuntimeError("crypto tab: no 'Total cryptocurrency (USD)' row")
    f = str((formulas[total_row - 1] + ["", ""])[1]) if len(formulas) >= total_row else ""
    refs = set()
    for a, b in re.findall(r"\$?B\$?(\d+)(?::\$?B\$?(\d+))?", f):
        refs.update(range(int(a), int(b or a) + 1))
    counted = [c for c, d in coins.items() if d["value_row"] in refs]
    if not counted:
        raise RuntimeError(f"crypto tab: can't map the total formula {f!r} to coin rows")
    return {"coins": coins, "total": total, "counted": counted, "formula": f}


# ---------------------------------------------------------------- BTC: BIP32 public derivation (pure python)

_P = 2**256 - 2**32 - 977
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
XPUB_VERSIONS = {  # version bytes -> address type
    bytes.fromhex("0488B21E"): "p2pkh",        # xpub  BIP44 legacy 1...
    bytes.fromhex("049D7CB2"): "p2sh-p2wpkh",  # ypub  BIP49 3...
    bytes.fromhex("04B24746"): "p2wpkh",       # zpub  BIP84 bc1q...
}


def _padd(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % _P == 0:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, _P) % _P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, _P) % _P
    x = (lam * lam - a[0] - b[0]) % _P
    return x, (lam * (a[0] - x) - a[1]) % _P


def _pmul(k: int, pt=_G):
    out = None
    while k:
        if k & 1:
            out = _padd(out, pt)
        pt = _padd(pt, pt)
        k >>= 1
    return out


def _ser(pt) -> bytes:
    return bytes([2 + (pt[1] & 1)]) + pt[0].to_bytes(32, "big")


def _deser(b: bytes):
    x = int.from_bytes(b[1:], "big")
    y = pow((x ** 3 + 7) % _P, (_P + 1) // 4, _P)
    if (y & 1) != (b[0] & 1):
        y = _P - y
    if b[0] not in (2, 3) or (y * y - x ** 3 - 7) % _P:
        raise ValueError("bad public key")
    return x, y


def _ripemd160(b: bytes) -> bytes:
    try:
        return hashlib.new("ripemd160", b).digest()
    except ValueError:  # OpenSSL 3 without the legacy provider
        return _ripemd160_py(b)


def _ripemd160_py(msg: bytes) -> bytes:
    """Pure-python RIPEMD-160 (only used if hashlib lacks it)."""
    rl = lambda x, n: ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF  # noqa: E731
    f = [lambda x, y, z: x ^ y ^ z, lambda x, y, z: (x & y) | (~x & z), lambda x, y, z: (x | ~y) ^ z,
         lambda x, y, z: (x & z) | (y & ~z), lambda x, y, z: x ^ (y | ~z)]
    K1, K2 = [0, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E], [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0]
    R1 = [list(range(16)), [7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8],
          [3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12], [1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2],
          [4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13]]
    R2 = [[5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12], [6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2],
          [15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13], [8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14],
          [12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11]]
    S1 = [[11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8], [7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12],
          [11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5], [11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12],
          [9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6]]
    S2 = [[8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6], [9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11],
          [9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5], [15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8],
          [8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11]]
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    msg = msg + b"\x80" + b"\x00" * ((55 - len(msg)) % 64) + (8 * len(msg)).to_bytes(8, "little")
    for off in range(0, len(msg), 64):
        X = [int.from_bytes(msg[off + 4 * j: off + 4 * j + 4], "little") for j in range(16)]
        a1, b1, c1, d1, e1 = h
        a2, b2, c2, d2, e2 = h
        for rnd in range(5):
            for j in range(16):
                t = rl((a1 + (f[rnd](b1, c1, d1) & 0xFFFFFFFF) + X[R1[rnd][j]] + K1[rnd]) & 0xFFFFFFFF, S1[rnd][j])
                a1, e1, d1, c1, b1 = e1, d1, rl(c1, 10), b1, (t + e1) & 0xFFFFFFFF
                t = rl((a2 + (f[4 - rnd](b2, c2, d2) & 0xFFFFFFFF) + X[R2[rnd][j]] + K2[rnd]) & 0xFFFFFFFF, S2[rnd][j])
                a2, e2, d2, c2, b2 = e2, d2, rl(c2, 10), b2, (t + e2) & 0xFFFFFFFF
        h = [(h[1] + c1 + d2) & 0xFFFFFFFF, (h[2] + d1 + e2) & 0xFFFFFFFF, (h[3] + e1 + a2) & 0xFFFFFFFF,
             (h[4] + a1 + b2) & 0xFFFFFFFF, (h[0] + b1 + c2) & 0xFFFFFFFF]
    return b"".join(x.to_bytes(4, "little") for x in h)


def hash160(b: bytes) -> bytes:
    return _ripemd160(hashlib.sha256(b).digest())


def b58check_encode(payload: bytes) -> str:
    data = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    n, out = int.from_bytes(data, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def b58check_decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    data = n.to_bytes((n.bit_length() + 7) // 8, "big")
    data = b"\0" * (len(s) - len(s.lstrip("1"))) + data
    payload, chk = data[:-4], data[-4:]
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != chk:
        raise ValueError("bad base58 checksum")
    return payload


def _bech32_polymod(values) -> int:
    gen, chk = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3], 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def segwit_v0_address(hrp: str, program: bytes) -> str:
    """BIP173 bech32 for witness version 0."""
    acc, bits, data = 0, 0, [0]
    for b in program:
        acc, bits = (acc << 8) | b, bits + 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    hrpx = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    pm = _bech32_polymod(hrpx + data + [0] * 6) ^ 1
    cs = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    return hrp + "1" + "".join(cs[d] for d in data + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


def parse_xpub(xpub: str) -> tuple[bytes, bytes, str]:
    """-> (compressed pubkey, chain code, address type)."""
    raw = b58check_decode(xpub.strip())
    if len(raw) != 78 or raw[:4] not in XPUB_VERSIONS:
        raise ValueError("not an xpub/ypub/zpub (single-sig mainnet)")
    key = raw[45:78]
    _deser(key)
    return key, raw[13:45], XPUB_VERSIONS[raw[:4]]


def ckd_pub(key: bytes, chain: bytes, i: int) -> tuple[bytes, bytes]:
    """BIP32 public child derivation (non-hardened)."""
    if i >= 2**31:
        raise ValueError("hardened derivation needs the private key")
    I = hmac.new(chain, key + i.to_bytes(4, "big"), hashlib.sha512).digest()
    il = int.from_bytes(I[:32], "big")
    if il >= _N:
        raise ValueError("invalid child, skip index")
    pt = _padd(_pmul(il), _deser(key))
    if pt is None:
        raise ValueError("invalid child, skip index")
    return _ser(pt), I[32:]


def address(pubkey: bytes, kind: str) -> str:
    h = hash160(pubkey)
    if kind == "p2pkh":
        return b58check_encode(b"\x00" + h)
    if kind == "p2sh-p2wpkh":
        return b58check_encode(b"\x05" + hash160(b"\x00\x14" + h))
    if kind == "p2wpkh":
        return segwit_v0_address("bc", h)
    raise ValueError(kind)


def xpub_addresses(xpub: str, change: int, start: int, count: int, kind: str | None = None) -> list[str]:
    key, chain, default_kind = parse_xpub(xpub)
    ckey, cchain = ckd_pub(key, chain, change)
    out = []
    for i in range(start, start + count):
        out.append(address(ckd_pub(ckey, cchain, i)[0], kind or default_kind))
    return out


def scan_xpub(xpub: str, lookup, gap: int = 20, kind: str | None = None) -> dict:
    """Walk receive (0) and change (1) chains until `gap` consecutive never-used addresses.
    lookup(addr) -> (balance_sats, tx_count). Returns {sats, used, checked}."""
    sats = used = checked = 0
    key, chain, default_kind = parse_xpub(xpub)
    kind = kind or default_kind
    for change in (0, 1):
        ckey, cchain = ckd_pub(key, chain, change)
        i, unused = 0, 0
        while unused < gap:
            try:
                addr = address(ckd_pub(ckey, cchain, i)[0], kind)
            except ValueError:  # astronomically rare invalid child: BIP32 says skip it
                i += 1
                continue
            bal, txs = lookup(addr)
            checked += 1
            if txs:
                used, unused, sats = used + 1, 0, sats + bal
            else:
                unused += 1
            i += 1
    return {"sats": sats, "used": used, "checked": checked, "kind": kind}


class BtcApi:
    """mempool.space Esplora API with blockstream.info as fallback; ~4 req/s, backs off on 429."""
    BASES = ["https://mempool.space/api", "https://blockstream.info/api"]

    def __init__(self, delay: float = 0.25):
        self.delay, self.base = delay, 0

    def __call__(self, addr: str) -> tuple[int, int]:
        last = None
        for attempt in range(6):
            time.sleep(self.delay * (1 if attempt == 0 else 4 * attempt))
            try:
                d = _req(f"{self.BASES[self.base]}/address/{addr}")
                c, m = d["chain_stats"], d["mempool_stats"]
                return (c["funded_txo_sum"] - c["spent_txo_sum"] + m["funded_txo_sum"] - m["spent_txo_sum"],
                        c["tx_count"] + m["tx_count"])
            except Exception as e:  # noqa: BLE001 - back off, then switch API
                last = e
                if attempt % 2 == 1:
                    self.base = (self.base + 1) % len(self.BASES)
        raise RuntimeError(f"BTC balance lookup failed: {last}")


# ---------------------------------------------------------------- other chains

ETH_RPCS = ["https://ethereum-rpc.publicnode.com", "https://eth.drpc.org", "https://rpc.flashbots.net",
            "https://cloudflare-eth.com"]
ERC20 = {"USDT": ("0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
         "USDC": ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6)}
TRC20 = {"USDT": ("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", 6)}
SOL_RPCS = ["https://api.mainnet-beta.solana.com", "https://solana-rpc.publicnode.com"]


def eth_balances(addr: str) -> dict[str, float]:
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        raise ValueError("bad ETH address")
    out = {"ETH": int(_rpc(ETH_RPCS, "eth_getBalance", [addr, "latest"]), 16) / 1e18}
    for sym, (token, dec) in ERC20.items():
        data = "0x70a08231" + addr[2:].lower().rjust(64, "0")
        out[sym] = int(_rpc(ETH_RPCS, "eth_call", [{"to": token, "data": data}, "latest"]) or "0x0", 16) / 10**dec
    return out


def trx_balances(addr: str) -> dict[str, float]:
    if not re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", addr):
        raise ValueError("bad TRON address")
    d = _req(f"https://api.trongrid.io/v1/accounts/{addr}")
    acct = (d.get("data") or [{}])[0]  # never-activated accounts come back empty
    out = {"TRX": acct.get("balance", 0) / 1e6}
    trc20 = {k: v for t in acct.get("trc20", []) for k, v in t.items()}
    for sym, (contract, dec) in TRC20.items():
        out[sym] = int(trc20.get(contract, 0)) / 10**dec
    return out


def sol_balances(addr: str) -> dict[str, float]:
    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", addr):
        raise ValueError("bad Solana address")
    return {"SOL": _rpc(SOL_RPCS, "getBalance", [addr])["value"] / 1e9}


# ---------------------------------------------------------------- exchanges (read-only keys)

def kraken_sign(urlpath: str, postdata: str, nonce: str, secret_b64: str) -> str:
    msg = urlpath.encode() + hashlib.sha256((nonce + postdata).encode()).digest()
    return base64.b64encode(hmac.new(base64.b64decode(secret_b64), msg, hashlib.sha512).digest()).decode()


def kraken_balances(key: str, secret: str) -> tuple[dict[str, float], dict]:
    path, nonce = "/0/private/Balance", str(int(time.time() * 1000))
    post = urllib.parse.urlencode({"nonce": nonce})
    try:
        d = _req("https://api.kraken.com" + path, post.encode(),
                 {"API-Key": key, "API-Sign": kraken_sign(path, post, nonce, secret),
                  "Content-Type": "application/x-www-form-urlencoded"})
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"kraken: HTTP {e.code}") from None
    if d.get("error"):
        raise RuntimeError(f"kraken: {d['error']}")
    out: dict[str, float] = {}
    for code, amt in d.get("result", {}).items():  # XBT.M / ETH2.S / USDT.F (earn, staked) fold into the coin
        c = norm_coin(code)
        out[c] = out.get(c, 0.0) + float(amt)
    return out, {k: v for k, v in d.get("result", {}).items() if float(v)}


def gemini_sign(payload: dict, secret: str) -> tuple[str, str]:
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    return b64, hmac.new(secret.encode(), b64.encode(), hashlib.sha384).hexdigest()


def gemini_balances(key: str, secret: str, account: str = "primary") -> tuple[dict[str, float], dict]:
    """POST /v1/balances; time-based nonce in epoch seconds (must be within 30 s of Gemini's clock). A master
    (account-group) key must name the account it reads; an account-level key must not."""
    payload = {"request": "/v1/balances", "nonce": str(int(time.time()))}
    if key.startswith("master-"):
        payload["account"] = account
    b64, sig = gemini_sign(payload, secret)
    try:
        rows = _req("https://api.gemini.com/v1/balances", b"",
                    {"Content-Type": "text/plain", "Content-Length": "0", "Cache-Control": "no-cache",
                     "X-GEMINI-APIKEY": key, "X-GEMINI-PAYLOAD": b64, "X-GEMINI-SIGNATURE": sig})
    except urllib.error.HTTPError as e:
        try:  # only the short error code ("InvalidApiKey", "InvalidSignature"...), never the message text
            reason = str(json.loads(e.read()).get("reason", ""))[:40]
        except Exception:  # noqa: BLE001
            reason = ""
        raise RuntimeError(f"gemini: HTTP {e.code} {reason}".strip()) from None
    out: dict[str, float] = {}
    for r in rows:
        # "amount" leaves out an ACH deposit Gemini has advanced for trading but not yet cleared (it can go
        # negative after a buy); that money has already left the bank, so it is counted via "available"
        c = norm_coin(r["currency"])
        out[c] = out.get(c, 0.0) + max(float(r["amount"]), float(r.get("available") or r["amount"]))
    return out, {r["currency"]: r["amount"] for r in rows if float(r["amount"])}


# ---------------------------------------------------------------- config

TEMPLATE = {
    "_help": "Optional live sources for valuations/crypto.py. Never commit/share this file. Each list entry "
             "names the wallet row it replaces on the sheet's crypto tab.",
    "btc": [],
    "_btc_example": [{"wallet": "Desktop", "xpub": "zpub6r... (or xpub/ypub)"},
                     {"wallet": "Mobile", "address": "bc1q..."}],
    "eth": [],
    "_eth_example": [{"wallet": "Desktop", "address": "0x... (ETH + ERC-20 USDT/USDC)"}],
    "trx": [],
    "_trx_example": [{"wallet": "Desktop", "address": "T... (TRX + TRC-20 USDT)"}],
    "sol": [],
    "_sol_example": [{"wallet": "Mobile", "address": "base58 address"}],
    "gemini": {"api_key": "", "api_secret": "", "_note": "read-only key, Fund Management: balances only"},
    "kraken": {"api_key": "", "api_secret": "", "_note": "read-only key, Query Funds only"},
}


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(TEMPLATE, f, indent=2)
    if path.stat().st_mode & 0o077:
        os.chmod(path, 0o600)
    return json.loads(path.read_text())


def _crypto_cfg() -> dict:
    return personal.section("crypto")


def exchange_rows() -> set[str]:
    """Wallet names that have their own balance-sheet row (personal.json crypto.exchange_rows)."""
    return EXCHANGE_ROWS if EXCHANGE_ROWS is not None else set(_crypto_cfg().get("exchange_rows") or [])


def default_wallet() -> str:
    """Wallet name for config entries that don't name one (personal.json crypto.default_wallet)."""
    return _crypto_cfg().get("default_wallet") or "Wallet"


def xpub_export_dir() -> Path | None:
    if XPUB_EXPORT_DIR is not None:
        return XPUB_EXPORT_DIR
    d = _crypto_cfg().get("xpub_export_dir")
    return Path(d).expanduser() if d else None


def export_entries(folder: Path | None = None, wallet: str | None = None) -> list[dict]:
    """xpub/ypub/zpub keys in wallet-export .txt files -> btc config entries for `wallet`
    (personal.json crypto.xpub_export_wallet). Addresses are not auto-imported: transaction exports also
    contain other people's addresses."""
    out, seen, folder = [], set(), folder or xpub_export_dir()
    if not folder or not folder.is_dir():
        return out
    wallet = wallet or _crypto_cfg().get("xpub_export_wallet") or default_wallet()
    for p in sorted(folder.glob("*.txt")):
        for k in re.findall(r"\b[xyz]pub[1-9A-HJ-NP-Za-km-z]{100,112}\b", p.read_text(errors="ignore")):
            if k not in seen:
                try:
                    parse_xpub(k)
                except ValueError:
                    continue
                seen.add(k)
                out.append({"wallet": wallet, "xpub": k, "_from": "xpub-exports"})
    return out


# ---------------------------------------------------------------- live quantity sources

def onchain_sources(cfg: dict, btc_lookup=None) -> tuple[list[dict], list[str]]:
    """-> ([{wallet, source, coins, qty, note}], warnings). One entry per configured wallet+chain."""
    out, warns, failed = [], [], set()
    btc = list(cfg.get("btc") or [])
    def key_id(x):  # the same account exported as xpub and zpub has the same key + chain code
        try:
            return parse_xpub(x)[:2]
        except ValueError:
            return x
    have = {key_id(e["xpub"]) for e in btc if e.get("xpub")}
    btc += [e for e in export_entries() if key_id(e["xpub"]) not in have]
    lookup = btc_lookup or BtcApi()
    for n, e in enumerate(btc, 1):
        w = e.get("wallet") or default_wallet()
        try:
            if e.get("xpub"):
                kinds = list(e.get("scripts") or [None])
                sats, used, checked, names = 0, 0, 0, []
                while kinds:
                    r = scan_xpub(e["xpub"], lookup, gap=int(e.get("gap", 20)), kind=kinds.pop(0))
                    sats, used, checked = sats + r["sats"], used + r["used"], checked + r["checked"]
                    names.append(r["kind"])
                    # a plain "xpub" is BIP44 legacy by spec, but some wallets export segwit accounts as xpub too
                    if not used and not kinds and not e.get("scripts") and e["xpub"].startswith("xpub") \
                            and "p2wpkh" not in names:
                        kinds = ["p2wpkh", "p2sh-p2wpkh"]
                if not used:  # almost always a wrong key or script type; don't zero the wallet
                    raise ValueError(f"no used addresses ({'+'.join(names)}); set \"scripts\" if the type is wrong")
                note = f"{e['xpub'][:4]} #{n} ({'+'.join(names)}): {used} used of {checked} addresses checked"
            else:
                sats, txs = lookup(e["address"])
                note = f"address #{n}"
            out.append({"wallet": w, "chain": "btc", "source": "onchain", "coins": {"BTC"}, "qty": {"BTC": sats / 1e8},
                        "note": note})
        except Exception as ex:  # noqa: BLE001 - keep the sheet quantity for this wallet
            warns.append(f"BTC {w} #{n}: {ex}")
            failed.add((wallet_name(w), "btc"))
    for chain, fn in (("eth", eth_balances), ("trx", trx_balances), ("sol", sol_balances)):
        for n, e in enumerate(cfg.get(chain) or [], 1):
            w = e.get("wallet") or default_wallet()
            try:
                q = fn(e["address"])
                out.append({"wallet": w, "chain": chain, "source": "onchain", "coins": set(q), "qty": q,
                            "note": f"{chain} address #{n} ...{e['address'][-4:]}"})
            except Exception as ex:  # noqa: BLE001
                warns.append(f"{chain.upper()} {w} #{n}: {ex}")
                failed.add((wallet_name(w), chain))
    # a partial result would undercount: if any entry of a wallet+chain failed, that wallet keeps its sheet qty
    for w, chain in sorted(failed):
        warns.append(f"{chain.upper()} {w}: lookup failed, using the sheet quantity")
    return [e for e in out if (wallet_name(e["wallet"]), e["chain"]) not in failed], warns


def exchange_sources(cfg: dict) -> tuple[list[dict], list[str]]:
    out, warns = [], []
    for w, fn in (("Gemini", gemini_balances), ("Kraken", kraken_balances)):
        k = cfg.get(w.lower()) or {}
        if not (k.get("api_key") and k.get("api_secret")):
            continue
        try:
            qty, raw = fn(k["api_key"], k["api_secret"])
            out.append({"wallet": w, "source": "exchange-api", "coins": None, "qty": qty,
                        "note": "private balance API, raw codes " + json.dumps(raw)})
        except Exception as ex:  # noqa: BLE001 - message never contains the key
            warns.append(f"{w} API: {ex}")
    return out, warns


def simplefin_sources(db_path: Path | None = None) -> list[dict]:
    """Crypto-kind accounts synced by SimpleFIN, grouped per institution."""
    db = sqlite3.connect(f"file:{db_path or DB}?mode=ro", uri=True)
    try:
        accts = db.execute("SELECT account_id, institution, balance, as_of FROM v_accounts WHERE kind='crypto'").fetchall()
        hold = db.execute("SELECT account_id, symbol, shares FROM v_holdings_latest").fetchall()
    finally:
        db.close()
    by_acct: dict[str, list] = {}
    for aid, sym, shares in hold:
        by_acct.setdefault(aid, []).append((sym, shares))
    wallets: dict[str, dict] = {}
    for aid, inst, bal, as_of in accts:
        w = wallets.setdefault(inst, {"wallet": inst, "source": "simplefin", "coins": None, "qty": {},
                                      "as_of": set()})
        w["as_of"].add((as_of or "")[:10])
        rows = [(norm_coin(s), float(q or 0)) for s, q in by_acct.get(aid, []) if s]
        if not rows and bal:  # cash-only wallet (an exchange's USD wallet)
            rows = [("USD", float(bal))]
        for c, q in rows:
            w["qty"][c] = w["qty"].get(c, 0.0) + q
    for w in wallets.values():
        w["note"] = "SimpleFIN as of " + ", ".join(sorted(d for d in w.pop("as_of") if d))
    return list(wallets.values())


# ---------------------------------------------------------------- prices

def prices(coins: set[str], sheet_prices: dict[str, float]) -> tuple[dict[str, tuple[float, str]], list[str]]:
    out: dict[str, tuple[float, str]] = {c: (1.0, "par") for c in coins if c == "USD"}
    warns = []
    want = sorted(c for c in coins if c in COINS)
    try:
        ids = ",".join(COINS[c][0] for c in want)
        cg = _req(f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd")
        for c in want:
            px = (cg.get(COINS[c][0]) or {}).get("usd")
            if px and px > 0:
                out[c] = (float(px), "coingecko")
    except Exception as e:  # noqa: BLE001
        warns.append(f"coingecko: {e}")
    kr: dict[str, float] = {}
    for c in want:  # one call per pair: Kraken rejects a whole batch if any pair is unknown/delisted
        try:
            d = _req(f"https://api.kraken.com/0/public/Ticker?pair={COINS[c][1]}")
            res = d.get("result") or {}
            kr[c] = float((res.get(COINS[c][2]) or next(iter(res.values())))["c"][0])
        except Exception:  # noqa: BLE001 - not listed on Kraken
            continue
    for c in want:
        if c in out and c in kr and abs(kr[c] / out[c][0] - 1) > 0.05:
            warns.append(f"{c}: coingecko {out[c][0]} vs kraken {kr[c]} differ >5%")
        if c not in out and c in kr:
            out[c] = (kr[c], "kraken")
    for c in coins - set(out):
        try:
            d = _req(f"https://api.coinbase.com/v2/prices/{c}-USD/spot")
            out[c] = (float(d["data"]["amount"]), "coinbase")
        except Exception:  # noqa: BLE001
            if sheet_prices.get(c):
                out[c] = (sheet_prices[c], "sheet price")
                warns.append(f"{c}: no live price, using the sheet's {sheet_prices[c]}")
    return out, warns


# ---------------------------------------------------------------- combine

def combine(sheet: dict, live: list[dict], px: dict[str, tuple[float, str]]) -> dict:
    """Pick the best quantity per (wallet, coin) and value it. Pure (no I/O)."""
    pairs: dict[tuple[str, str], float] = {}
    for c, d in sheet["coins"].items():
        for w, q in d["wallets"].items():
            pairs[(w, c)] = q
    live_by_wallet: dict[str, list[dict]] = {}
    for e in live:
        live_by_wallet.setdefault(wallet_name(e["wallet"]), []).append(e)
        for c in e["qty"]:
            pairs.setdefault((wallet_name(e["wallet"]), c), 0.0)
    counted_coins = set(sheet["counted"])
    exchanges = exchange_rows()
    rows, missing = [], []
    for (w, c), sheet_q in sorted(pairs.items()):
        q, src, notes = sheet_q, "sheet", []
        for rank in SOURCE_RANK:
            hits = [e for e in live_by_wallet.get(w, []) if e["source"] == rank and (e["coins"] is None or c in e["coins"])]
            if hits:
                q, src = sum(e["qty"].get(c, 0.0) for e in hits), rank
                notes = [e["note"] for e in hits if e.get("note")]
                break
        if abs(q) < 1e-12 and abs(sheet_q) < 1e-12:
            continue
        p, psrc = px.get(c, (None, None))
        # at the exchange rows only the (wallet, coin) rows the sheet's total counts; the rest is in their own row.
        # An exchange read through its API counts every coin, and its USD becomes that row (exchange_cash)
        if w in exchanges and src == "exchange-api":
            counted = c != "USD"
        else:
            counted = (c in counted_coins and w in sheet["coins"].get(c, {}).get("wallets", {})) \
                if w in exchanges else True
        if p is None:
            missing.append(f"{w} {c}")
            p, counted = 0.0, False
        rows.append({"wallet": w, "coin": c, "qty": round(q, 8), "price": p, "price_source": psrc,
                     "value": round(q * p, 2), "qty_source": src, "sheet_qty": sheet_q, "counted": counted,
                     **({"note": "; ".join(notes)} if notes else {}),
                     **({} if counted else {"why_not_counted": f"in the balance sheet's own '{w}' row"})})
    warns = [f"no price for {m}, not counted" for m in missing]
    by_coin: dict[str, dict] = {}
    for r in rows:
        if r["counted"]:
            b = by_coin.setdefault(r["coin"], {"qty": 0.0, "price": r["price"], "value": 0.0})
            b["qty"] = round(b["qty"] + r["qty"], 8)
            b["value"] = round(b["value"] + r["value"], 2)
    total = round(sum(r["value"] for r in rows if r["counted"]), 2)
    sheet_wallets = {w for d in sheet["coins"].values() for w in d["wallets"]}
    unmatched = sorted({wallet_name(e["wallet"]) for e in live if any(abs(q) > 1e-12 for q in e["qty"].values())}
                       - sheet_wallets)
    cash = {r["wallet"]: r["value"] for r in rows
            if r["wallet"] in exchanges and r["qty_source"] == "exchange-api" and r["coin"] == "USD"}
    cash.update({wallet_name(e["wallet"]): cash.get(wallet_name(e["wallet"]), 0.0) for e in live
                 if e["source"] == "exchange-api" and wallet_name(e["wallet"]) in exchanges})
    return {"total": total, "rows": rows, "by_coin": dict(sorted(by_coin.items(), key=lambda kv: -kv[1]["value"])),
            "unmatched": unmatched, "warnings": warns, "exchange_cash": cash}


def sheet_reproduction(sheet: dict) -> float:
    """The sheet's formula: sum over counted coins of (sum of wallet qty) x sheet price."""
    return sum(sum(d["wallets"].values()) * (d["price"] or 0) for c, d in sheet["coins"].items()
               if c in sheet["counted"])


def fetch() -> list[dict]:
    sheet = parse_sheet(_sheet("crypto!A1:D200"), _sheet("crypto!A1:D200", "FORMULA"))
    repro = sheet_reproduction(sheet)
    if sheet["total"] is None or abs(repro - sheet["total"]) > 1.0:
        raise RuntimeError(f"crypto tab parse gives {repro:,.2f} but the sheet's total is {sheet['total']}")

    cfg = load_config()
    on, w1 = onchain_sources(cfg)
    ex, w2 = exchange_sources(cfg)
    try:
        sf, w3 = simplefin_sources(), []
    except sqlite3.Error as e:
        sf, w3 = [], [f"simplefin: {e}"]
    live = on + ex + sf
    coins = {c for c in sheet["coins"]} | {c for e in live for c in e["qty"]}
    sheet_px = {c: d["price"] for c, d in sheet["coins"].items() if d["price"]}
    px, w4 = prices(coins, sheet_px)
    res = combine(sheet, live, px)

    at_sheet_qty_live_px = sum(sum(d["wallets"].values()) * px[c][0] for c, d in sheet["coins"].items()
                               if c in sheet["counted"])
    wallets = {}
    for r in res["rows"]:
        wallets.setdefault(r["wallet"], set()).add(r["qty_source"])
    srcs = sorted({r["qty_source"] for r in res["rows"] if r["counted"]}, key=lambda s: (SOURCE_RANK + ("sheet",)).index(s))
    return [{
        "asset": "Total cryptocurrency (USD)",
        "value": res["total"],
        "source": "qty: " + "/".join(srcs) + "; prices: " + ", ".join(sorted({r["price_source"] for r in res["rows"]
                                                                          if r["price_source"]})),
        "detail": {
            "holdings": res["rows"],
            "by_coin": res["by_coin"],
            "wallet_sources": {w: sorted(s) for w, s in sorted(wallets.items())},
            "sheet_total": sheet["total"],
            "sheet_total_formula": sheet["formula"],
            "sheet_total_counts": sheet["counted"],
            "sheet_reproduced_at_sheet_qty_and_prices": round(repro, 2),
            "at_sheet_qty_live_prices_sheet_coins": round(at_sheet_qty_live_px, 2),
            "prices": {c: {"usd": p, "source": s} for c, (p, s) in sorted(px.items())},
            "sheet_prices": sheet_px,
            "warnings": w1 + w2 + w3 + w4 + res["warnings"] + [
                f"live wallet {w!r} has no row on the crypto tab (typo in crypto_wallets.json?)" for w in res["unmatched"]],
        },
    }] + [{"asset": w, "value": round(usd, 2), "source": "exchange-api USD balance",  # the exchange's own sheet row
           "detail": {"note": "cash at the exchange, incl. deposits credited for trading that have not cleared"}}
          for w, usd in sorted(res["exchange_cash"].items())]


if __name__ == "__main__":
    print(json.dumps(fetch(), indent=1, default=sorted))
