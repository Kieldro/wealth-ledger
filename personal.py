"""Owner-specific settings, kept out of the repo.

Everything that identifies the owner (spreadsheet id, balance-sheet row labels, account-name patterns, extra
classification rules, home/vehicle details, wallet names, ...) lives in a JSON file outside the repo:

    ~/.config/wealth-ledger/personal.json      (override with $WEALTH_LEDGER_PERSONAL)

Start from personal.example.json in the repo root, copy it there and `chmod 600` it.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

DEFAULT_PATH = "~/.config/wealth-ledger/personal.json"


class PersonalConfigError(RuntimeError):
    pass


def path() -> Path:
    return Path(os.environ.get("WEALTH_LEDGER_PERSONAL") or DEFAULT_PATH).expanduser()


@lru_cache(maxsize=None)
def _load(p: str) -> dict:
    f = Path(p)
    if not f.exists():
        raise PersonalConfigError(
            f"{f} not found. Copy personal.example.json from the wealth-ledger repo there, "
            f"chmod 600 it and fill in your own values (or set WEALTH_LEDGER_PERSONAL).")
    try:
        data = json.loads(f.read_text())
    except ValueError as e:
        raise PersonalConfigError(f"{f} is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise PersonalConfigError(f"{f} must hold a JSON object")
    return data


def load() -> dict:
    """The whole personal.json (cached per path). Raises PersonalConfigError if it is missing or invalid."""
    return _load(str(path()))


def section(name: str) -> dict:
    """One top-level object of personal.json, or {} if absent."""
    return load().get(name) or {}


def require(name: str, key: str):
    """section(name)[key], with a clear error naming the missing key."""
    sec = section(name)
    if key not in sec:
        raise PersonalConfigError(f"{path()}: missing \"{name}.{key}\" (see personal.example.json)")
    return sec[key]


def has_sheet() -> bool:
    """False for a ledger that runs on synced accounts alone (no "sheet_id"): nothing then reads a spreadsheet."""
    return bool(load().get("sheet_id"))


def sheet_id() -> str:
    sid = load().get("sheet_id")
    if not sid:
        raise PersonalConfigError(f"{path()}: missing \"sheet_id\" (see personal.example.json)")
    return sid
