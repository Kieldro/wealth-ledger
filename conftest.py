"""pytest: run every suite against personal.example.json, never the owner's real personal.json."""
import os
from pathlib import Path

os.environ["WEALTH_LEDGER_PERSONAL"] = str(Path(__file__).resolve().parent / "personal.example.json")
