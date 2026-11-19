"""Crypto transfers from a made-up Exodus export, send log and on-chain data (no real data)."""
import json
from datetime import datetime, timezone

import pytest

import ctdb
from insights import crypto_transfers as ct

HEADER = ("DATE,TYPE,FROMPORTFOLIO,TOPORTFOLIO,OUTAMOUNT,OUTCURRENCY,FEEAMOUNT,FEECURRENCY,FROMADDRESS,TOADDRESS,"
          "OUTTXID,OUTTXURL,INAMOUNT,INCURRENCY,INTXID,INTXURL,ORDERID,PERSONALNOTE\n")
ROWS = [
    "2026-03-01T18:00:00.000Z,deposit,,exodus_0,,,,,,,,,0.5,BTC,aaa,u,,from exchange\n",
    "2026-03-02T18:00:00.000Z,withdrawal,exodus_0,,-0.01,BTC,-0.0001,BTC,,bc1qmine,bbb,u,,,,,,gift\n",
    "2026-03-03T18:00:00.000Z,withdrawal,exodus_1,,-0.02,BTC,-0.0001,BTC,,bc1qother,ccc,u,,,,,,\n",
]


@pytest.fixture
def con(tmp_path):
    return ctdb.connect(tmp_path / "t.db")


def write_csv(tmp_path, rows=ROWS):
    f = tmp_path / "exodus_0-all-bitcoin-txs-2026-03-04_00-00-00.csv"
    f.write_text(HEADER + "".join(rows), encoding="utf-8")
    return f


def test_exodus_rows_directions_portfolios_and_local_time(tmp_path, monkeypatch):
    monkeypatch.setenv("WEALTH_LEDGER_TZ", "America/Denver")
    rows = ct.exodus_rows(write_csv(tmp_path))
    assert [(r["wallet"], r["direction"], r["amount"], r["fee"], r["memo"]) for r in rows] == [
        ("Exodus", "in", 0.5, None, "from exchange"), ("Exodus", "out", 0.01, 0.0001, "gift"),
        ("Exodus 2", "out", 0.02, 0.0001, "")]
    assert rows[0]["date"] == "2026-03-01T11:00:00-07:00"


def test_timezone_from_env_then_config_else_this_machine(monkeypatch):
    utc = "2026-03-01T18:00:00.000Z"
    monkeypatch.delenv("WEALTH_LEDGER_TZ", raising=False)
    monkeypatch.setattr(ct.crypto, "_crypto_cfg", lambda: {})
    assert ct.tz() is None
    assert ct._local(utc) == datetime(2026, 3, 1, 18, tzinfo=timezone.utc).astimezone().isoformat(timespec="seconds")
    monkeypatch.setattr(ct.crypto, "_crypto_cfg", lambda: {"timezone": "Asia/Tokyo"})
    assert ct._local(utc) == "2026-03-02T03:00:00+09:00"
    monkeypatch.setenv("WEALTH_LEDGER_TZ", "UTC")
    assert ct._local(utc) == "2026-03-01T18:00:00+00:00"


def test_send_log_follows_fee_bumps(tmp_path):
    log = tmp_path / "sends.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in [
        {"time": "2026-03-05T10:00:00-06:00", "txid": "t1", "to": "bc1qfriend", "note": "gift", "btc": "0.001",
         "usd": "80", "fee_btc": "0.00000141"},
        {"time": "2026-03-05T10:05:00-06:00", "type": "bump", "replaces": "t1", "txid": "t2", "usd": "0",
         "fee_btc": "0.000005"}]) + "\n")
    [r] = ct.send_log_rows(log, "Spending")
    assert (r["txid"], r["fee"], r["memo"], r["wallet"]) == ("t2", 0.000005, "gift", "Spending")
    assert ct.send_log_rows(None, "Spending") == [] and ct.send_log_rows(tmp_path / "none.jsonl", "Spending") == []


def test_send_log_is_off_unless_configured(tmp_path, monkeypatch):
    log = tmp_path / "sends.jsonl"
    log.write_text(json.dumps({"time": "2026-03-05T10:00:00-06:00", "txid": "t1", "to": "bc1qfriend", "btc": "0.001"}))
    monkeypatch.setattr(ct.crypto, "_crypto_cfg", lambda: {})
    assert ct.send_log() == (None, "Wallet") and ct.gather(None, {}) == []
    monkeypatch.setattr(ct.crypto, "_crypto_cfg", lambda: {"send_log": str(log), "send_log_wallet": "Spending"})
    assert ct.send_log() == (log, "Spending")
    assert [(r["wallet"], r["txid"], r["source"]) for r in ct.gather(None, {})] == [("Spending", "t1", "send-log")]


def test_send_to_own_wallet_is_labelled_and_mirrored():
    rows = ct.link_own([{"wallet": "Exodus", "coin": "BTC", "txid": "bbb", "direction": "out", "date": "d",
                         "amount": 0.01, "fee": 0.0001, "address": "bc1qmine", "memo": "top up", "source": "x"}],
                       {"bc1qmine": "Spending"})
    assert [(r["wallet"], r["direction"], r.get("counterparty"), r["fee"]) for r in rows] == [
        ("Exodus", "out", "Spending", 0.0001), ("Spending", "in", "Exodus", None)]


def test_onchain_net_amounts_and_self_moves():
    mine = {"m1", "m2"}
    def tx(txid, vin, vout, fee):
        return {"txid": txid, "fee": fee, "status": {"confirmed": True, "block_time": 1772000000},
                "vin": [{"prevout": {"scriptpubkey_address": a, "value": v}} for a, v in vin],
                "vout": [{"scriptpubkey_address": a, "value": v} for a, v in vout]}
    txs = {t["txid"]: t for t in [
        tx("recv", [("ext", 60000)], [("m1", 50000), ("ext2", 9000)], 1000),
        tx("send", [("m1", 50000)], [("friend", 20000), ("m2", 29000)], 1000),
        tx("self", [("m2", 29000)], [("m1", 28500)], 500)]}
    rows = {r["txid"]: r for r in ct.onchain_rows("W", mine, txs)}
    assert set(rows) == {"recv", "send"}
    assert (rows["recv"]["direction"], rows["recv"]["amount"]) == ("in", 0.0005)
    assert (rows["send"]["direction"], rows["send"]["amount"], rows["send"]["fee"], rows["send"]["address"]) == (
        "out", 0.0002, 0.00001, "friend")


def test_reimport_keeps_memo_and_export_wins_over_chain(con, tmp_path, monkeypatch):
    monkeypatch.setattr(ct.crypto, "_crypto_cfg", lambda: {"xpub_export_wallet": "Exodus"})
    chain = [{"wallet": "Exodus", "coin": "BTC", "txid": "bbb", "direction": "out", "date": "2026-03-02T13:00:00",
              "amount": 0.01, "fee": 0.0001, "address": "bc1qmine", "memo": "", "source": "onchain"}]
    write_csv(tmp_path)
    ct.store(con, ct.gather(tmp_path, {}, tmp_path / "none.jsonl", chain=chain))
    write_csv(tmp_path, [ROWS[1].replace(",gift\n", ",\n")])  # a later export where the memo was cleared
    ct.store(con, ct.gather(tmp_path, {}, tmp_path / "none.jsonl", chain=chain))
    rows = con.execute("SELECT wallet, txid, memo, source FROM crypto_transfers ORDER BY txid").fetchall()
    assert rows == [("Exodus", "aaa", "from exchange", "exodus-csv"), ("Exodus", "bbb", "gift", "exodus-csv"),
                    ("Exodus 2", "ccc", "", "exodus-csv")]


def test_usd_value_from_daily_prices(con):
    con.execute("INSERT INTO prices VALUES ('2026-03-01','BTC',80000,'x')")
    ct.store(con, [{"wallet": "W", "coin": "BTC", "txid": "t", "direction": "in", "date": "2026-03-02T09:00:00-06:00",
                    "amount": 0.5, "fee": None, "source": "x"}])
    assert con.execute("SELECT price_usd, usd_value FROM v_crypto_transfers").fetchone() == (80000, 40000.0)


def _chain(n):
    return [{"txid": f"t{i}", "status": {"confirmed": True}} for i in range(n)]


@pytest.mark.parametrize("first, total", [(50, 130), (25, 130), (50, 50), (25, 75), (50, 12)])
def test_address_history_is_read_to_the_end_whatever_the_first_page_size(first, total):
    """mempool.space sends 50 confirmed transactions first (stock Esplora 25), then 25 per /txs/chain call."""
    chain, calls = _chain(total), []

    def get(path):
        calls.append(path)
        if path.endswith("/txs"):
            return [{"txid": "pending", "status": {"confirmed": False}}] + chain[:first]
        at = [t["txid"] for t in chain].index(path.rsplit("/", 1)[1]) + 1
        return chain[at:at + 25]

    got = [t["txid"] for t in ct.address_txs("addr", get)]
    assert got == ["pending"] + [t["txid"] for t in chain] and len(calls) <= 2 + total // 25
