#!/usr/bin/env python3
"""Crypto transfers (sends and receives) with their memos, from wallet exports and send logs.

Sources:
  * On-chain history of every bitcoin xpub in crypto_wallets.json and the Exodus export folder (mempool.space, the
    same API the balance check uses): every send and receive, wherever it was made (desktop, phone), no export needed.
  * Exodus transaction-history CSVs ("exodus_0-all-bitcoin-txs-<date>.csv", one per coin) in the export folder
    (personal.json crypto.xpub_export_dir). Columns DATE, TYPE, FROM/TOPORTFOLIO, OUT/INAMOUNT, OUT/INCURRENCY,
    FEEAMOUNT, TOADDRESS, OUT/INTXID, ORDERID, PERSONALNOTE. Exodus has no API, so its memos ("PERSONALNOTE") only
    leave the app this way.
  * An optional send log (personal.json crypto.send_log, booked to the wallet named by crypto.send_log_wallet): a
    JSON-lines file that a script or hot wallet appends to, one send per line {"time": ISO time, "txid", "to",
    "btc", "fee_btc", "note"}, whose "note" is the memo; {"type": "bump", "replaces", "txid", "fee_btc"} lines move
    a send to its fee-bumped replacement txid.
A send to an address of another of your own bitcoin wallets (an xpub in crypto_wallets.json) is labelled with that
wallet as counterparty and also recorded as a receive in it. Re-importing is idempotent; a memo is never replaced
by an empty one. USD values come from the daily prices table (v_crypto_transfers). Times are stored in the
timezone named by $WEALTH_LEDGER_TZ or personal.json crypto.timezone (an IANA name such as Europe/Berlin); default:
this machine's local time.

  python -m insights.crypto_transfers            import everything found
  python -m insights.crypto_transfers --dry-run  show what would be imported
Never sends notifications.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ctdb  # noqa: E402
from valuations import crypto  # noqa: E402

DEFAULT_DB = Path(os.environ.get("WEALTH_LEDGER_DB") or "~/.local/share/wealth-ledger/txns.db").expanduser()
OWN_ADDRESS_SCAN = 100  # receive + change addresses checked per own xpub
GAP = 20  # unused addresses in a row that end an xpub walk (as in valuations/crypto.py)
UPSERT = """INSERT INTO crypto_transfers (wallet, coin, txid, direction, date, amount, fee, address, counterparty, memo,
            source) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (wallet, coin, txid, direction) DO UPDATE SET date = excluded.date, amount = excluded.amount,
              fee = COALESCE(excluded.fee, fee), address = COALESCE(excluded.address, address),
              counterparty = COALESCE(excluded.counterparty, counterparty),
              memo = COALESCE(NULLIF(excluded.memo, ''), memo), source = excluded.source"""


def tz() -> tzinfo | None:
    """The timezone transfer times are stored in: $WEALTH_LEDGER_TZ, else personal.json crypto.timezone (IANA names),
    else None, which is this machine's local time."""
    name = os.environ.get("WEALTH_LEDGER_TZ") or crypto._crypto_cfg().get("timezone")
    return ZoneInfo(name) if name else None


def send_log() -> tuple[Path | None, str]:
    """(file, wallet name) of the optional send log: personal.json crypto.send_log and crypto.send_log_wallet."""
    cfg = crypto._crypto_cfg()
    return (Path(cfg["send_log"]).expanduser() if cfg.get("send_log") else None,
            cfg.get("send_log_wallet") or crypto.default_wallet())


def _local(iso: str) -> str:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz()).isoformat(timespec="seconds")


def _num(v: str) -> float | None:
    return abs(float(v)) if (v or "").strip() else None


def _wallet(portfolio: str, base: str) -> str:
    """exodus_0 -> 'Exodus', exodus_1 -> 'Exodus 2' (Exodus numbers its portfolios from 0)."""
    n = (portfolio or "").rsplit("_", 1)[-1]
    return base if not n.isdigit() or n == "0" else f"{base} {int(n) + 1}"


def exodus_rows(path: Path, base: str = "Exodus") -> list[dict]:
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            when, memo = _local(r["DATE"]), (r.get("PERSONALNOTE") or "").strip()
            if _num(r.get("OUTAMOUNT")):
                out.append({"wallet": _wallet(r.get("FROMPORTFOLIO"), base), "coin": r["OUTCURRENCY"],
                            "txid": r.get("OUTTXID") or r.get("ORDERID"), "direction": "out", "date": when,
                            "amount": _num(r["OUTAMOUNT"]), "fee": _num(r.get("FEEAMOUNT")),
                            "address": r.get("TOADDRESS") or None, "memo": memo, "source": "exodus-csv"})
            if _num(r.get("INAMOUNT")):
                out.append({"wallet": _wallet(r.get("TOPORTFOLIO"), base), "coin": r["INCURRENCY"],
                            "txid": r.get("INTXID") or r.get("ORDERID"), "direction": "in", "date": when,
                            "amount": _num(r["INAMOUNT"]), "fee": None, "address": r.get("FROMADDRESS") or None,
                            "memo": memo, "source": "exodus-csv"})
    return [x for x in out if x["txid"] and x["coin"]]


def send_log_rows(path: Path | None, wallet: str) -> list[dict]:
    if not path or not path.exists():
        return []
    sends, replaced = {}, {}
    for line in path.read_text().splitlines():
        e = json.loads(line) if line.strip() else None
        if not e:
            continue
        if e.get("type") == "bump":
            replaced[e["replaces"]] = e
        else:
            sends[e["txid"]] = e
    out = []
    for txid, e in sends.items():
        fee = float(e.get("fee_btc") or 0) or None
        while txid in replaced:  # follow fee bumps to the transaction that actually confirms
            b = replaced.pop(txid)
            txid, fee = b["txid"], float(b.get("fee_btc") or 0) or fee
        out.append({"wallet": wallet, "coin": "BTC", "txid": txid, "direction": "out",
                    "date": e["time"], "amount": float(e["btc"]), "fee": fee, "address": e.get("to"),
                    "memo": e.get("note") or "", "source": "send-log"})
    return out


def own_addresses(cfg: dict | None = None) -> dict[str, str]:
    """address -> wallet name, for every bitcoin xpub you own (receive and change, first OWN_ADDRESS_SCAN each)."""
    cfg = crypto.load_config() if cfg is None else cfg
    out = {}
    for e in (cfg.get("btc") or []) + crypto.export_entries():
        if not e.get("xpub"):
            continue
        name = crypto.wallet_name(e.get("wallet") or crypto.default_wallet())
        try:
            for change in (0, 1):
                for a in crypto.xpub_addresses(e["xpub"], change, 0, OWN_ADDRESS_SCAN):
                    out[a] = name
        except ValueError:
            continue
    return out


def xpub_used_addresses(xpub: str, lookup) -> list[str]:
    """Used addresses of an xpub (receive and change), walking each chain until GAP unused in a row."""
    key, chain, kind = crypto.parse_xpub(xpub)
    used = []
    for change in (0, 1):
        ckey, cchain = crypto.ckd_pub(key, chain, change)
        i = unused = 0
        while unused < GAP:
            addr = crypto.address(crypto.ckd_pub(ckey, cchain, i)[0], kind)
            if lookup(addr)[1]:
                used.append(addr)
                unused = 0
            else:
                unused += 1
            i += 1
    return used


def address_txs(addr: str, get) -> list[dict]:
    """Every transaction of an address. The first call gives the mempool plus the newest confirmed ones: 50 on
    mempool.space, 25 on stock Esplora; /txs/chain/<last txid> then gives 25 older ones per call. Keep asking
    until a page comes back short or brings nothing new, whatever the first page's size was."""
    txs = list(get(f"/address/{addr}/txs"))
    seen = {t["txid"] for t in txs}
    page = [t for t in txs if t["status"].get("confirmed")]
    while len(page) >= 25:
        page = get(f"/address/{addr}/txs/chain/{page[-1]['txid']}")
        new = [t for t in page if t["txid"] not in seen]
        if not new:
            break
        seen.update(t["txid"] for t in new)
        txs += new
    return txs


def onchain_rows(wallet: str, addrs: set[str], txs: dict[str, dict]) -> list[dict]:
    """One row per transaction: net BTC in or out of `addrs`; a send's amount excludes its fee."""
    out = []
    for t in txs.values():
        spent = sum(v["prevout"]["value"] for v in t["vin"]
                    if (v.get("prevout") or {}).get("scriptpubkey_address") in addrs)
        got = sum(o["value"] for o in t["vout"] if o.get("scriptpubkey_address") in addrs)
        when = (datetime.fromtimestamp(t["status"]["block_time"], timezone.utc) if t["status"].get("block_time")
                else datetime.now(timezone.utc)).astimezone(tz()).isoformat(timespec="seconds")
        if spent:
            sent = spent - got - t["fee"]
            if sent <= 0:  # moved between its own addresses: only the fee left the wallet
                continue
            dest = next((o.get("scriptpubkey_address") for o in t["vout"]
                         if o.get("scriptpubkey_address") not in addrs), None)
            out.append({"wallet": wallet, "coin": "BTC", "txid": t["txid"], "direction": "out", "date": when,
                        "amount": sent / 1e8, "fee": t["fee"] / 1e8, "address": dest, "memo": "", "source": "onchain"})
        elif got:
            src = next((v["prevout"].get("scriptpubkey_address") for v in t["vin"] if v.get("prevout")), None)
            out.append({"wallet": wallet, "coin": "BTC", "txid": t["txid"], "direction": "in", "date": when,
                        "amount": got / 1e8, "fee": None, "address": src, "memo": "", "source": "onchain"})
    return out


def onchain(cfg: dict | None = None, api=None, get=None) -> list[dict]:
    """Transfers of every bitcoin xpub you own, grouped per wallet (several xpubs of one wallet are one wallet)."""
    cfg = crypto.load_config() if cfg is None else cfg
    api = api or crypto.BtcApi()
    get = get or (lambda path: (crypto.time.sleep(0.25), crypto._req("https://mempool.space/api" + path))[1])
    by_wallet: dict[str, list[str]] = {}
    for e in (cfg.get("btc") or []) + crypto.export_entries():
        if e.get("xpub"):
            by_wallet.setdefault(crypto.wallet_name(e.get("wallet") or crypto.default_wallet()), []).append(e["xpub"])
    rows = []
    for wallet, xpubs in by_wallet.items():
        addrs, txs = set(), {}
        for x in dict.fromkeys(xpubs):
            for a in xpub_used_addresses(x, api):
                addrs.add(a)
                for t in address_txs(a, get):
                    txs[t["txid"]] = t
        rows += onchain_rows(wallet, addrs, txs)
    return rows


def link_own(rows: list[dict], own: dict[str, str]) -> list[dict]:
    """Label sends to your own wallets, and add the matching receive on the other wallet."""
    extra = []
    for r in rows:
        dest = own.get(r.get("address") or "")
        if r["direction"] == "out" and dest and dest != crypto.wallet_name(r["wallet"]):
            r["counterparty"] = dest
            extra.append({**r, "wallet": dest, "direction": "in", "fee": None, "address": r["address"],
                          "counterparty": crypto.wallet_name(r["wallet"]), "source": r["source"] + " (own)"})
    return rows + extra


def gather(folder: Path | None, own: dict[str, str], log: Path | None = None,
           chain: list[dict] | None = None) -> list[dict]:
    rows = list(chain or [])  # on-chain first, so exports and logs (with memos and send times) win
    folder = folder or crypto.xpub_export_dir()
    if folder and folder.exists():
        for f in sorted(folder.glob("exodus*-txs-*.csv"), key=lambda p: p.stat().st_mtime):  # newest memo wins
            rows += exodus_rows(f, crypto._export_wallet(None))
    cfg_log, wallet = send_log()
    rows += send_log_rows(log or cfg_log, wallet)
    return link_own(rows, own)


def store(con, rows: list[dict]) -> int:
    con.executemany(UPSERT, [(r["wallet"], r["coin"], r["txid"], r["direction"], r["date"], r["amount"], r["fee"],
                              r.get("address"), r.get("counterparty"), r.get("memo") or "", r["source"])
                             for r in rows])
    con.commit()
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-onchain", action="store_true", help="exports and send logs only (no API calls)")
    args = ap.parse_args(argv)
    try:
        rows = gather(None, own_addresses(), chain=[] if args.no_onchain else onchain())
    except (OSError, ValueError, KeyError, csv.Error) as e:
        print(f"error: crypto transfers: {e}", file=sys.stderr)
        return 1
    memos = sum(1 for r in rows if r.get("memo"))
    if args.dry_run:
        for r in rows[:20]:
            print(r["date"][:10], r["wallet"], r["direction"], r["amount"], r["coin"], r.get("counterparty") or "",
                  r.get("memo") or "")
    else:
        store(ctdb.connect(args.db), rows)
    print(f"crypto transfers: {len(rows)} {'would be ' if args.dry_run else ''}stored, {memos} with a memo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
