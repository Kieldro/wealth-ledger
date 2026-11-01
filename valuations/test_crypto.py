"""Offline tests for valuations/crypto.py: BTC derivation vectors, signing vectors, sheet parse, source precedence.

  cd ~/wealth-ledger && .venv/bin/python -m pytest valuations/test_crypto.py -q
"""
import hashlib
import hmac
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from valuations import crypto as c  # noqa: E402

EXCHANGES = {"Alphax", "Betax"}


@pytest.fixture(autouse=True)
def _no_personal_data(monkeypatch, tmp_path):
    """Never read the owner's personal.json or wallet exports from tests."""
    monkeypatch.setattr(c, "EXCHANGE_ROWS", EXCHANGES)
    monkeypatch.setattr(c, "XPUB_EXPORT_DIR", tmp_path / "no-exports")


# BIP84/49/44 test mnemonic "abandon x11 about" account-0 extended public keys and first addresses
ZPUB = "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"
YPUB = "ypub6Ww3ibxVfGzLrAH1PNcjyAWenMTbbAosGNB6VvmSEgytSER9azLDWCxoJwW7Ke7icmizBMXrzBx9979FfaHxHcrArf3zbeJJJUZPf663zsP"
XPUB = "xpub6BosfCnifzxcFwrSzQiqu2DBVTshkCXacvNsWGYJVVhhawA7d4R5WSWGFNbi8Aw6ZRc1brxMyWMzG3DSSSSoekkudhUd9yLb6qx39T9nMdj"
MNEMONIC = " ".join(["abandon"] * 11 + ["about"])


def test_bip84_zpub_addresses():
    assert c.xpub_addresses(ZPUB, 0, 0, 2) == ["bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu",
                                               "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g"]
    assert c.xpub_addresses(ZPUB, 1, 0, 1) == ["bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"]


def test_bip49_ypub_address():
    assert c.xpub_addresses(YPUB, 0, 0, 1) == ["37VucYSaXLCAsxYyAPfbSi9eh4iEcbShgf"]


def test_bip44_xpub_address():
    assert c.xpub_addresses(XPUB, 0, 0, 1) == ["1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA"]


def test_xpub_script_override():
    # same key, forced to p2wpkh: matches what a segwit wallet exporting plain "xpub" would use
    assert c.xpub_addresses(XPUB, 0, 0, 1, "p2wpkh")[0].startswith("bc1q")


def test_bip32_vector1_public_ckd():
    # BIP32 test vector 1: public derivation from m/0H gives the published m/0H/1 xpub
    m0h = "xpub68Gmy5EdvgibQVfPdqkBBCHxA5htiqg55crXYuXoQRKfDBFA1WEjWgP6LHhwBZeNK1VTsfTFUHCdrfp1bgwQ9xv5ski8PX9rL2dZXvgGDnw"
    want = "xpub6ASuArnXKPbfEwhqN6e3mwBcDTgzisQN1wXN9BJcM47sSikHjJf3UFHKkNAWbWMiGj7Wf5uMash7SyYq527Hqck2AxYysAA7xmALppuCkwQ"
    key, chain, _ = c.parse_xpub(m0h)
    ck, cc = c.ckd_pub(key, chain, 1)
    raw = c.b58check_decode(want)
    assert (ck, cc) == (raw[45:], raw[13:45])


def _priv_to_account_zpub(mnemonic: str, path: list[int]) -> str:
    """Independent check: BIP39 seed -> private BIP32 derivation -> serialized zpub."""
    seed = hashlib.pbkdf2_hmac("sha512", mnemonic.encode(), b"mnemonic", 2048)
    I = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    k, ch, parent_fp = int.from_bytes(I[:32], "big"), I[32:], b"\0" * 4
    for i in path:
        pub = c._ser(c._pmul(k))
        data = (b"\0" + k.to_bytes(32, "big")) if i >= 2**31 else pub
        I = hmac.new(ch, data + i.to_bytes(4, "big"), hashlib.sha512).digest()
        parent_fp = c.hash160(pub)[:4]
        k, ch = (int.from_bytes(I[:32], "big") + k) % c._N, I[32:]
    payload = bytes.fromhex("04B24746") + bytes([len(path)]) + parent_fp + path[-1].to_bytes(4, "big") + ch + c._ser(c._pmul(k))
    return c.b58check_encode(payload)


def test_zpub_vector_matches_mnemonic():
    H = 2**31
    assert _priv_to_account_zpub(MNEMONIC, [84 + H, 0 + H, 0 + H]) == ZPUB


def test_ripemd160_fallback():
    assert c._ripemd160_py(b"").hex() == "9c1185a5c5e9fc54612808977ee8f548b2258d31"
    assert c._ripemd160_py(b"abc").hex() == "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc"
    assert c._ripemd160_py(b"a" * 1000) == hashlib.new("ripemd160", b"a" * 1000).digest()


def test_bad_xpub_rejected():
    with pytest.raises(ValueError):
        c.parse_xpub(ZPUB[:-1] + ("t" if ZPUB[-1] != "t" else "u"))


def test_gap_limit_scan():
    used = {c.xpub_addresses(ZPUB, 0, 0, 30)[i]: (1000 * (i + 1)) for i in (0, 5, 24)}
    used[c.xpub_addresses(ZPUB, 1, 0, 1)[0]] = 7
    calls = []

    def lookup(a):
        calls.append(a)
        return (used[a], 1) if a in used else (0, 0)

    r = c.scan_xpub(ZPUB, lookup, gap=20)
    assert r["sats"] == 1000 + 6000 + 25000 + 7 and r["used"] == 4
    assert r["checked"] == (25 + 20) + (1 + 20)  # receive stops at index 44, change at 20


def test_kraken_signature_vector():
    # example from Kraken's REST API authentication docs
    secret = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
    post = "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25"
    assert c.kraken_sign("/0/private/AddOrder", post, "1616492376594", secret) == \
        "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="


def test_gemini_signature_formula():
    # Gemini docs: X-GEMINI-SIGNATURE = hex(HMAC_SHA384(base64(json payload), key=api_secret))
    b64, sig = c.gemini_sign({"request": "/v1/order/status", "nonce": 123456, "order_id": 18834}, "1234abcd")
    assert b64 == "eyJyZXF1ZXN0IjogIi92MS9vcmRlci9zdGF0dXMiLCAibm9uY2UiOiAxMjM0NTYsICJvcmRlcl9pZCI6IDE4ODM0fQ=="
    assert sig == hmac.new(b"1234abcd", b64.encode(), hashlib.sha384).hexdigest() and len(sig) == 96


@pytest.mark.parametrize("code,want", [("XXBT", "BTC"), ("XBT.M", "BTC"), ("XXMR", "XMR"), ("ETH2.S", "ETH"),
                                       ("ZUSD", "USD"), ("XXDG", "DOGE"), ("TRX", "TRX"), ("USDT", "USDT"),
                                       ("btc", "BTC"), ("GUSD", "USD"), ("XETH", "ETH"), ("ALGO", "ALGO")])
def test_norm_coin(code, want):
    assert c.norm_coin(code) == want


# synthetic crypto tab (UNFORMATTED + the total's formula); same layout as a real tab, made-up wallets and amounts.
# Alphax and Betax play the exchanges that have their own balance-sheet rows.
VALUES = [
    ["Cryptocurrency", "current", "usd value", "percent of total"], ["BTC price", 50000], ["Hodl", 0.5],
    ["Alphax", 0.25], ["Brokerx", 0.1], ["Mobile", 0.001], ["Total BTC", 0.851],
    ["Total BTC value (USD)", 42550.0], [], ["XMR price", 200], ["Privacy wallet", 10], ["Betax", 2.5],
    ["Total XMR", 12.5], ["Total XMR value (USD)", 2500.0], [], ["ETH price", 3000], ["Hodl", 1.5], ["Alphax", 0],
    ["Total ETH", 1.5], ["Total ETH value (USD)", 4500.0], [], ["TRX"], ["price", 0.25], ["Hodl", 100],
    ["Betax", 2000], ["Total", 2100], ["Total value (USD)", 525.0], ["USDT"], ["price", 1], ["Betax", 250], [],
    ["Total", 250], ["Total value (USD)", 250], [], [], ["SOL price", 100], ["Hodl", 0.5],
    ["Mobile wallet (old)", 0.25], ["Total SOL", 0.75], ["Total SOL value (USD)", 75.0], [],
    ["BCH price", 400], ["Hodl wallet", 0.2], ["Total BCH", 0.2], ["Total BCH value (USD)", 80.0], [],
    ["LTC price", 80], ["Hodl wallet", 2], ["Total LTC", 2], ["Total LTC value (USD)", 160], [],
    ["ALGO price", 0.2], ["Hodl", 100], ["Total ALGO", 100], ["Total ALGO value (USD)", 20.0], [],
    ["DOGE price", 0.1], ["Hodl", 1000], ["Alphax", 0], ["Total DOGE", 1000], ["Total DOGE value (USD)", 100.0],
    [], ["Total cryptocurrency (USD)", 49905.0],
]
FORMULAS = [[]] * 62 + [["Total cryptocurrency (USD)", "=B20+B8+B14+B50+B40+B55+B61"]]


def test_parse_sheet_reproduces_total():
    s = c.parse_sheet(VALUES, FORMULAS)
    assert sorted(s["counted"]) == ["ALGO", "BTC", "DOGE", "ETH", "LTC", "SOL", "XMR"]
    assert s["coins"]["TRX"]["wallets"] == {"Hodl": 100, "Betax": 2000}
    assert s["coins"]["SOL"]["wallets"] == {"Hodl": 0.5, "Mobile": 0.25}
    assert s["coins"]["USDT"]["price"] == 1
    assert abs(c.sheet_reproduction(s) - 49905.0) < 1e-6


def test_combine_precedence_and_exchange_rows():
    s = c.parse_sheet(VALUES, FORMULAS)
    px = {k: (d["price"], "sheet") for k, d in s["coins"].items()} | {"USD": (1.0, "par")}
    # all-sheet: counts everything the sheet counts + BCH and Hodl TRX; Betax TRX/USDT left to the "Betax" row
    base = c.combine(s, [], px)
    extra = 100 * 0.25 + 0.2 * 400
    assert abs(base["total"] - (49905.0 + extra)) < 0.05
    assert not [r for r in base["rows"] if r["wallet"] == "Betax" and r["coin"] in ("TRX", "USDT") and r["counted"]]
    live = [
        {"wallet": "Brokerx", "source": "simplefin", "coins": None, "qty": {"BTC": 0.2, "USD": 0.36, "ETH": 0.0}},
        {"wallet": "Hodl", "source": "onchain", "coins": {"BTC"}, "qty": {"BTC": 0.5}},
        {"wallet": "Hodl", "source": "onchain", "coins": {"BTC"}, "qty": {"BTC": 0.1}},
        {"wallet": "Hodl", "source": "onchain", "coins": {"ETH", "USDT", "USDC"},
         "qty": {"ETH": 2.0, "USDT": 10.0, "USDC": 0.0}},
        {"wallet": "Betax", "source": "exchange-api", "coins": None, "qty": {"XMR": 5.0, "USD": 50.0}},
    ]
    px |= {"USDC": (1.0, "x")}
    r = {(x["wallet"], x["coin"]): x for x in c.combine(s, live, px)["rows"]}
    assert r[("Brokerx", "BTC")]["qty"] == 0.2 and r[("Brokerx", "BTC")]["qty_source"] == "simplefin"
    assert r[("Brokerx", "USD")]["counted"]
    assert r[("Hodl", "BTC")]["qty"] == 0.6 and r[("Hodl", "BTC")]["qty_source"] == "onchain"
    assert r[("Hodl", "ETH")]["qty"] == 2.0 and r[("Hodl", "USDT")]["counted"]
    assert r[("Hodl", "LTC")]["qty_source"] == "sheet"
    assert r[("Betax", "XMR")]["qty"] == 5.0 and r[("Betax", "XMR")]["counted"]
    assert r[("Betax", "TRX")]["qty"] == 0 and r[("Betax", "TRX")]["qty_source"] == "exchange-api"
    assert not r[("Betax", "USD")]["counted"]
    assert r[("Alphax", "BTC")]["qty_source"] == "sheet"


def test_config_template_created_0600(tmp_path):
    p = tmp_path / "sub" / "crypto_wallets.json"
    cfg = c.load_config(p)
    assert cfg["btc"] == [] and (p.stat().st_mode & 0o777) == 0o600


def test_xpub_exports_parse(tmp_path):
    (tmp_path / "bitcoin.txt").write_text(f"Bitcoin (BTC)\nxpub: {ZPUB}\nbad: zpub{'1' * 107}\n")
    (tmp_path / "dupe.txt").write_text(ZPUB)
    assert c.export_entries(tmp_path, "Hodl") == [{"wallet": "Hodl", "xpub": ZPUB, "_from": "xpub-exports"}]


def test_live_wallet_names_normalized_like_sheet():
    s = c.parse_sheet(VALUES, FORMULAS)
    px = {k: (d["price"], "sheet") for k, d in s["coins"].items()}
    live = [{"wallet": "Hodl wallet", "source": "onchain", "coins": {"BTC"}, "qty": {"BTC": 0.5}},
            {"wallet": "betax pro", "source": "exchange-api", "coins": None, "qty": {"TRX": 10.0}}]
    res = c.combine(s, live, px)
    btc = [r for r in res["rows"] if r["coin"] == "BTC" and r["wallet"].startswith("Hodl")]
    assert len(btc) == 1 and btc[0]["qty"] == 0.5  # no double count with the sheet's "Hodl" row
    assert [r for r in res["rows"] if r["wallet"] == "Betax" and r["coin"] == "TRX"][0]["counted"]  # read by API
    assert res["unmatched"] == []


def test_failed_chain_falls_back_to_sheet(monkeypatch, tmp_path):
    monkeypatch.setattr(c, "XPUB_EXPORT_DIR", tmp_path)
    good = c.xpub_addresses(ZPUB, 0, 0, 1)[0]

    def lookup(a):
        if a == "bc1qbad":
            raise RuntimeError("HTTP 429")
        return (5000, 1) if a == good else (0, 0)

    cfg = {"btc": [{"wallet": "Hodl", "xpub": ZPUB, "gap": 2}, {"wallet": "Hodl", "address": "bc1qbad"},
                   {"wallet": "Mobile", "xpub": ZPUB, "gap": 2}]}
    out, warns = c.onchain_sources(cfg, btc_lookup=lookup)
    assert [e["wallet"] for e in out] == ["Mobile"] and out[0]["qty"]["BTC"] == 5000 / 1e8
    assert any("Hodl: lookup failed" in w for w in warns)


def test_total_formula_ranges():
    f = [[]] * 62 + [["Total cryptocurrency (USD)", "=SUM($B$8,B14:B20)+B61"]]
    assert sorted(c.parse_sheet(VALUES, f)["counted"]) == ["BTC", "DOGE", "ETH", "XMR"]


def test_exchange_rows_counted_per_wallet_coin_and_unpriced_not_fatal():
    s = c.parse_sheet(VALUES, FORMULAS)
    px = {k: (d["price"], "sheet") for k, d in s["coins"].items()}
    live = [{"wallet": "Betax", "source": "exchange-api", "coins": None,
             "qty": {"XMR": 1.0, "BTC": 0.1, "XXLM": 50.0}}]
    res = c.combine(s, live, px)
    r = {(x["wallet"], x["coin"]): x for x in res["rows"]}
    assert r[("Betax", "XMR")]["counted"] and r[("Betax", "BTC")]["counted"]  # read by API: every priced coin
    assert not r[("Betax", "XXLM")]["counted"] and res["warnings"] == ["no price for Betax XXLM, not counted"]


def test_xpub_with_no_used_addresses_is_a_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(c, "XPUB_EXPORT_DIR", tmp_path)
    seen = []
    out, warns = c.onchain_sources({"btc": [{"wallet": "Hodl", "xpub": XPUB, "gap": 2}]},
                                   btc_lookup=lambda a: seen.append(a[0]) or (0, 0))
    assert out == [] and any("no used addresses" in w for w in warns)
    assert {"1", "b", "3"} <= set(seen)  # plain xpub: legacy first, then segwit retries


def test_xpub_export_dedup_by_key(monkeypatch, tmp_path):
    # same account given as zpub in config and re-encoded as xpub in an export -> scanned once
    raw = c.b58check_decode(ZPUB)
    as_xpub = c.b58check_encode(bytes.fromhex("0488B21E") + raw[4:])
    (tmp_path / "btc.txt").write_text(as_xpub)
    monkeypatch.setattr(c, "XPUB_EXPORT_DIR", tmp_path)
    good = c.xpub_addresses(ZPUB, 0, 0, 1)[0]
    out, _ = c.onchain_sources({"btc": [{"wallet": "Hodl", "xpub": ZPUB, "gap": 1}]},
                               btc_lookup=lambda a: (100, 1) if a == good else (0, 0))
    assert len(out) == 1 and out[0]["qty"]["BTC"] == 100 / 1e8


def test_exchange_read_by_api_counts_all_coins_and_its_cash_is_its_own_row():
    s = c.parse_sheet(VALUES, FORMULAS)
    px = {k: (d["price"], "sheet") for k, d in s["coins"].items()} | {"USD": (1.0, "par"), "TRX": (0.3, "sheet")}
    live = [{"wallet": "Betax", "source": "exchange-api", "coins": None,
             "qty": {"XMR": 1.0, "BTC": 0.1, "TRX": 100.0, "USD": 713.0}}]
    res = c.combine(s, live, px)
    r = {(x["wallet"], x["coin"]): x for x in res["rows"]}
    assert r[("Betax", "BTC")]["counted"] and r[("Betax", "TRX")]["counted"] and not r[("Betax", "USD")]["counted"]
    assert res["exchange_cash"] == {"Betax": 713.0}


def test_gemini_advanced_deposit_counts_as_cash(monkeypatch):
    rows = [{"currency": "USD", "amount": "-330.56", "available": "1669.44"},
            {"currency": "BTC", "amount": "0.5", "available": "0.5"}]
    monkeypatch.setattr(c, "_req", lambda *a, **k: rows)
    qty, _ = c.gemini_balances("account-x", "secret")
    assert qty == {"USD": 1669.44, "BTC": 0.5}
