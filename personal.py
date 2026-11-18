"""Owner-specific settings, kept out of the repo.

Everything that identifies the owner (spreadsheet id, balance-sheet row labels, account-name patterns, extra
classification rules, home/vehicle details, wallet names, ...) lives in a JSON file outside the repo:

    ~/.config/wealth-ledger/personal.json

($WEALTH_LEDGER_PERSONAL names another file; $WEALTH_LEDGER_CONFIG_DIR another folder holding personal.json.)

The file is optional. Without it every setting takes its default: the ledger runs on its synced accounts alone,
account types are guessed from account names, and every add-on that needs a setting (spreadsheet, valuations,
backups, monthly email, ...) is off. personal.example.json in the repo root documents every key; copy the ones
you want and `chmod 600` the file.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

DEFAULT_DIR = "~/.config/wealth-ledger"


class PersonalConfigError(RuntimeError):
    pass


def path() -> Path:
    if os.environ.get("WEALTH_LEDGER_PERSONAL"):
        return Path(os.environ["WEALTH_LEDGER_PERSONAL"]).expanduser()
    return Path(os.environ.get("WEALTH_LEDGER_CONFIG_DIR") or DEFAULT_DIR).expanduser() / "personal.json"


@lru_cache(maxsize=None)
def _load(p: str) -> dict:
    f = Path(p)
    if not f.exists():  # optional: no file = every default
        return {}
    try:
        data = json.loads(f.read_text())
    except ValueError as e:
        raise PersonalConfigError(f"{f} is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise PersonalConfigError(f"{f} must hold a JSON object")
    return data


def load() -> dict:
    """The whole personal.json (cached per path); {} when there is no file. Raises PersonalConfigError if the
    file is there but invalid."""
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


def auto_kind() -> bool:
    """Whether account types are guessed from account names at each sync ("auto_kind"). Unset, it is on for a
    ledger without a spreadsheet and off for one with "sheet_id", whose owner classifies accounts by hand."""
    v = load().get("auto_kind")
    return (not has_sheet()) if v is None else bool(v)


def sheet_id() -> str:
    sid = load().get("sheet_id")
    if not sid:
        raise PersonalConfigError(f"{path()}: missing \"sheet_id\" (see personal.example.json)")
    return sid
