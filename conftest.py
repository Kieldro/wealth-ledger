"""pytest: run every suite against personal.example.json, never the owner's real personal.json."""
import os
from pathlib import Path

os.environ["WEALTH_LEDGER_PERSONAL"] = str(Path(__file__).resolve().parent / "personal.example.json")
# no live market prices from the network (valuations/live.py); tests that want some give them their own
os.environ["WEALTH_LEDGER_LIVE_PRICES"] = "off"
