"""pytest: run every suite against personal.example.json, never the owner's real personal.json."""
import os
from pathlib import Path

os.environ["CARD_TRACKER_PERSONAL"] = str(Path(__file__).resolve().parent / "personal.example.json")
