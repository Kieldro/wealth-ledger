#!/usr/bin/env python3
"""Inherited IRA tracker: the SECURE Act 10-year rule for an inherited (beneficiary) IRA.

For a non-spouse designated beneficiary who isn't an "eligible designated beneficiary", the account must be empty by
Dec 31 of the 10th year after the owner's death. If the owner had already reached their required beginning date
(had started RMDs), the beneficiary must also take an annual minimum in years 1-9, using IRS Pub 590-B Table I
(Single Life Expectancy): the beneficiary's age in the year after death, minus 1 for each later year (or the owner's
remaining life expectancy, if longer).

Shows the deadline, years left, the balance trend from daily snapshots, distributions taken this year, an even-spread
plan (flat, and level at an assumed growth rate) and the RMD estimate. Estimates only, not tax advice.

Facts about the owner live in personal.json (outside the repo), key "inherited_ira":
  account                LIKE pattern for v_accounts.account_name (default: names containing BDA/INHERITED/BENEFICIARY)
  death_year             year the original owner died
  owner_started_rmds     true/false: had the owner reached their required beginning date? null = unknown
  beneficiary_birth_year your birth year (for the life-expectancy factor)
  owner_birth_year       optional; the owner's remaining life expectancy is used if it is longer
  growth_rate            assumed annual return for the level plan (default 0.05)
  year_end_balances      optional {"YYYY": balance} when no Dec 31 snapshot exists (RMDs use the prior Dec 31 value)

  python -m insights.inherited_ira [--json]
Never sends notifications (owner's rule).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import personal  # noqa: E402

DB = Path(os.environ.get("WEALTH_LEDGER_DB") or Path.home() / ".local/share/wealth-ledger/txns.db")
KEY = "inherited_ira"
DEFAULT_GROWTH = 0.05
AUTO_NAMES = ("%BDA%", "%INHERITED%", "%BENEFICIARY%")
# money leaving the account: distributions (cash or to another account) and tax withheld from them
DIST_LIKE = ("%DISTRIBUTION%", "%TRANSFERRED TO%", "%WITHDRAWAL%", "%W/H%", "%WITHHOLD%", "%WITHHELD%")
NOT_DIST = ("YOU SOLD%", "YOU BOUGHT%", "%CORE ACCOUNT%", "REINVESTMENT%", "DIVIDEND%", "FEE CHARGED%",
            "FOREIGN TAX%")
SOURCE = ("IRS Publication 590-B, Distributions from IRAs: 10-year rule, \"Owner Died on or After Required "
          "Beginning Date\" and Appendix B Table I (Single Life Expectancy). https://www.irs.gov/publications/p590b")

# IRS Pub 590-B Appendix B, Table I (Single Life Expectancy, for use by beneficiaries; 2022+ table). Age -> factor.
_T1 = [84.6, 83.7, 82.8, 81.8, 80.8, 79.8, 78.8, 77.9, 76.9, 75.9, 74.9, 73.9, 72.9, 71.9, 70.9, 69.9, 69.0, 68.0,
       67.0, 66.0, 65.0, 64.1, 63.1, 62.1, 61.1, 60.2, 59.2, 58.2, 57.3, 56.3, 55.3, 54.4, 53.4, 52.5, 51.5, 50.5,
       49.6, 48.6, 47.7, 46.7, 45.7, 44.8, 43.8, 42.9, 41.9, 41.0, 40.0, 39.0, 38.1, 37.1, 36.2, 35.3, 34.3, 33.4,
       32.5, 31.6, 30.6, 29.8, 28.9, 28.0, 27.1, 26.2, 25.4, 24.5, 23.7, 22.9, 22.0, 21.2, 20.4, 19.6, 18.8, 18.0,
       17.2, 16.4, 15.6, 14.8, 14.1, 13.3, 12.6, 11.9, 11.2, 10.5, 9.9, 9.3, 8.7, 8.1, 7.6, 7.1, 6.6, 6.1, 5.7, 5.3,
       4.9, 4.6, 4.3, 4.0, 3.7, 3.4, 3.2, 3.0, 2.8, 2.6, 2.5, 2.3, 2.2, 2.1, 2.1, 2.1, 2.0, 2.0, 2.0, 2.0, 2.0, 1.9,
       1.9, 1.8, 1.8, 1.6, 1.4, 1.1, 1.0]
SINGLE_LIFE = dict(enumerate(_T1))  # 0..120 (120 = "120 and over")


def life_expectancy(age: int) -> float:
    return SINGLE_LIFE[min(max(age, 0), 120)]


# ---------- settings ----------
def load_settings() -> tuple[dict, str]:
    """personal.json's inherited_ira section, read fresh each call (so edits show without a restart)."""
    p = personal.path()
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}, str(p)
    sec = data.get(KEY) if isinstance(data, dict) else None
    return (sec if isinstance(sec, dict) else {}), str(p)


def _year(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 1900 <= v <= 2100 else None


def parse_settings(raw: dict, where: str = "personal.json") -> tuple[dict, list[dict]]:
    """Validated settings plus a list of {key, msg} for every missing or invalid fact."""
    missing = []
    s = {"account": raw.get("account") if isinstance(raw.get("account"), str) else None,
         "death_year": _year(raw.get("death_year")),
         "beneficiary_birth_year": _year(raw.get("beneficiary_birth_year")),
         "owner_birth_year": _year(raw.get("owner_birth_year")),
         "owner_started_rmds": raw.get("owner_started_rmds") if isinstance(raw.get("owner_started_rmds"), bool)
         else None,
         "year_end_balances": {}}
    g = raw.get("growth_rate", DEFAULT_GROWTH)
    s["growth_rate"] = float(g) if isinstance(g, (int, float)) and not isinstance(g, bool) and -0.5 < g < 0.5 \
        else DEFAULT_GROWTH
    for y, v in (raw.get("year_end_balances") or {}).items() if isinstance(raw.get("year_end_balances"), dict) else ():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            s["year_end_balances"][str(y)] = float(v)
    if s["death_year"] is None:
        missing.append({"key": "death_year", "msg": f"Set {KEY}.death_year (year the original owner died) in {where} "
                                                   "to get the deadline and plan."})
    if s["owner_started_rmds"] is None:
        missing.append({"key": "owner_started_rmds",
                        "msg": f"Set {KEY}.owner_started_rmds (true/false: had the owner started RMDs, i.e. reached "
                               f"their required beginning date?) in {where}. If true, an annual minimum is required "
                               "in years 1-9."})
    if s["beneficiary_birth_year"] is None:
        missing.append({"key": "beneficiary_birth_year",
                        "msg": f"Set {KEY}.beneficiary_birth_year in {where} for the life-expectancy factor."})
    return s, missing


# ---------- data ----------
def find_account(con: sqlite3.Connection, pattern: str | None = None) -> dict | None:
    pats = (pattern,) if pattern else AUTO_NAMES
    where = " OR ".join("UPPER(account_name) LIKE UPPER(?)" for _ in pats)
    r = con.execute(f"SELECT account_id, account_name, institution, balance, as_of, snapshot_date FROM v_accounts "
                    f"WHERE ({where}) ORDER BY ABS(balance) DESC LIMIT 1", pats).fetchone()
    return dict(zip(("account_id", "account_name", "institution", "balance", "as_of", "snapshot_date"), r)) if r else None


def balance_series(con, account_id: str) -> list[dict]:
    return [{"date": d, "balance": round(b or 0.0, 2)} for d, b in con.execute(
        "SELECT snapshot_date, MAX(CAST(balance AS REAL)) FROM balances WHERE account_id=? "
        "GROUP BY snapshot_date ORDER BY snapshot_date", (account_id,))]


def distributions(con, account_id: str, since: str, until: str | None = None) -> list[dict]:
    like = " OR ".join("UPPER(description) LIKE ?" for _ in DIST_LIKE)
    notlike = " AND ".join("UPPER(description) NOT LIKE ?" for _ in NOT_DIST)
    rows = con.execute(f"""SELECT date, amt, description, pending FROM v_ledger
                           WHERE account_id=? AND amt<0 AND date>=? AND date<=? AND ({like}) AND {notlike}
                           ORDER BY date""", (account_id, since, until or "9999-12-31", *DIST_LIKE, *NOT_DIST))
    return [{"date": d, "amount": round(-a, 2), "description": desc, "pending": bool(p)} for d, a, desc, p in rows]


def year_end_balance(con, account_id: str, year: int, cfg: dict) -> tuple[float | None, str]:
    """Dec 31 balance of `year`: a snapshot within the last week of the year, else personal.json."""
    r = con.execute("SELECT snapshot_date, CAST(balance AS REAL) FROM balances WHERE account_id=? "
                    "AND snapshot_date BETWEEN ? AND ? ORDER BY snapshot_date DESC LIMIT 1",
                    (account_id, f"{year}-12-25", f"{year}-12-31")).fetchone()
    if r:
        return round(r[1], 2), f"snapshot {r[0]}"
    v = cfg["year_end_balances"].get(str(year))
    if v is not None:
        return v, f"{KEY}.year_end_balances.{year} in personal.json"
    return None, ""


# ---------- math ----------
def level_payment(balance: float, n: int, rate: float) -> float:
    """Equal withdrawals at the start of each of n years that empty `balance` growing at `rate`."""
    if n <= 0:
        return balance
    if abs(rate) < 1e-9:
        return balance / n
    return balance * rate / ((1 - (1 + rate) ** -n) * (1 + rate))


def schedule(balance: float, start_year: int, end_year: int, rate: float, taken_this_year: float = 0.0) -> list[dict]:
    """Year-by-year level plan. This year's slot is net of what was already taken; balance grows after withdrawal."""
    n = end_year - start_year + 1
    if n <= 0:
        return []
    base = balance + taken_this_year  # roughly where this year started, so this year's slot counts what's taken
    pay = level_payment(base, n, rate)
    out, bal = [], balance
    for i, y in enumerate(range(start_year, end_year + 1)):
        w = max(0.0, pay - taken_this_year) if i == 0 else pay
        w = bal if y == end_year else min(w, bal)
        bal = (bal - w) * (1 + rate) if y != end_year else 0.0
        out.append({"year": y, "withdraw": round(w, 2), "end_balance": round(bal, 2)})
    return out


def rmd_factor(year: int, death_year: int, ben_birth: int, owner_birth: int | None) -> dict:
    first = death_year + 1
    ben_age = first - ben_birth
    ben = life_expectancy(ben_age) - (year - first)
    out = {"beneficiary_age_first_year": ben_age, "beneficiary_factor": round(ben, 1), "basis": "beneficiary"}
    if owner_birth:
        own = life_expectancy(death_year - owner_birth) - (year - death_year)
        out["owner_factor"] = round(own, 1)
        if own > ben:
            out["basis"] = "owner"
    out["factor"] = round(max(ben, out.get("owner_factor", 0.0)), 1)
    return out


# ---------- report ----------
def report(con: sqlite3.Connection, today: date | None = None, raw: dict | None = None,
           where: str | None = None) -> dict:
    today = today or date.today()
    if raw is None:
        raw, where = load_settings()
    cfg, missing = parse_settings(raw, where or "personal.json")
    y = today.year
    res = {"today": today.isoformat(), "year": y, "settings": {k: v for k, v in cfg.items() if k != "year_end_balances"},
           "settings_path": where, "missing": missing, "source": SOURCE,
           "assumptions": "Assumes a non-spouse designated beneficiary who is not an eligible designated beneficiary "
                          "(not a spouse, minor child, disabled/chronically ill, or within 10 years of the owner's age).",
           "notes": ["Estimates, not tax advice. Distributions are taxed as ordinary income in the year taken (no 10% "
                     "early-withdrawal penalty on inherited IRAs). Spreading them out usually keeps more of each year's "
                     "income in lower brackets than one large withdrawal near the deadline.",
                     "Shares can also be moved out in kind to a taxable account; that counts as a distribution at "
                     "market value, and may not appear as a cash withdrawal in the synced data."]}
    acct = find_account(con, cfg["account"])
    res["account"] = acct
    if not acct:
        res["error"] = (f"No inherited IRA account found (looked for {cfg['account'] or ', '.join(AUTO_NAMES)}). "
                        f"Set {KEY}.account to its v_accounts name pattern.")
        return res
    bal = round(acct["balance"] or 0.0, 2)

    # trend from daily snapshots; add back withdrawals in the window to show the market move
    series = balance_series(con, acct["account_id"])
    trend = {"series": series}
    if series:
        first, last = series[0], series[-1]
        out_in_window = sum(d["amount"] for d in distributions(con, acct["account_id"], first["date"], last["date"])
                            if d["date"] > first["date"])
        chg = last["balance"] - first["balance"]
        trend.update(since=first["date"], days=(date.fromisoformat(last["date"]) - date.fromisoformat(first["date"])).days,
                     change=round(chg, 2), change_pct=round(100 * chg / first["balance"], 2) if first["balance"] else None,
                     withdrawn=round(out_in_window, 2), market_change=round(chg + out_in_window, 2))
        cut = (date.fromisoformat(last["date"]) - timedelta(days=30)).isoformat()
        past = [s for s in series if s["date"] <= cut]
        trend["change_30d"] = round(last["balance"] - past[-1]["balance"], 2) if past else None
    res["trend"] = trend

    rows = distributions(con, acct["account_id"], f"{y}-01-01", f"{y}-12-31")
    taken = round(sum(r["amount"] for r in rows), 2)
    hist = con.execute("SELECT MIN(date) FROM v_ledger WHERE account_id=?", (acct["account_id"],)).fetchone()[0]
    res["this_year"] = {"year": y, "taken": taken, "rows": rows, "history_starts": hist,
                        "partial": bool(hist and hist > f"{y}-01-01")}

    dy = cfg["death_year"]
    if dy is None:
        return res
    deadline = dy + 10
    res["deadline"] = {"year": deadline, "date": f"{deadline}-12-31", "years_left": deadline - y,
                       "distribution_years_left": max(0, deadline - y + 1), "passed": y > deadline,
                       "first_year": dy + 1}
    if y > deadline:
        res["notes"].insert(0, f"The 10-year deadline (Dec 31, {deadline}) has passed; anything left is subject to the "
                               "excess-accumulation excise tax. Talk to a tax professional.")
        return res
    start = max(y, dy + 1)
    rate = cfg["growth_rate"]
    flat = schedule(bal, start, deadline, 0.0, taken if start == y else 0.0)
    grow = schedule(bal, start, deadline, rate, taken if start == y else 0.0)
    res["plan"] = {"growth_rate": rate, "years": deadline - start + 1,
                   "flat_annual": round(level_payment(bal + taken, deadline - start + 1, 0.0), 2),
                   "growth_annual": round(level_payment(bal + taken, deadline - start + 1, rate), 2),
                   "this_year_remaining_flat": flat[0]["withdraw"] if start == y else None,
                   "this_year_remaining_growth": grow[0]["withdraw"] if start == y else None,
                   "schedule": [{"year": f["year"], "flat": f["withdraw"], "growth": g["withdraw"],
                                 "end_balance_growth": g["end_balance"]} for f, g in zip(flat, grow)]}

    # annual RMD (only years 1-9; the deadline year empties the account anyway)
    rmd = {"status": {True: "required", False: "not_required", None: "unknown"}[cfg["owner_started_rmds"]], "year": y}
    if y < dy + 1:
        rmd["note"] = f"The first beneficiary year is {dy + 1}."
    elif y >= deadline:
        rmd["note"] = f"{deadline} is the deadline year: the whole balance must come out by Dec 31."
    elif cfg["owner_started_rmds"] is False:
        rmd["note"] = ("The owner died before their required beginning date, so no annual minimum is required; only "
                       "the 10-year deadline applies.")
    elif cfg["beneficiary_birth_year"] is None:
        rmd["note"] = "Need beneficiary_birth_year to estimate the annual minimum."
    else:
        f = rmd_factor(y, dy, cfg["beneficiary_birth_year"], cfg["owner_birth_year"])
        basis, basis_src = year_end_balance(con, acct["account_id"], y - 1, cfg)
        exact = basis is not None
        if not exact:
            basis, basis_src = round(bal + taken, 2), (f"rough: no Dec 31, {y - 1} balance on file, so today's balance "
                                                      f"plus this year's withdrawals; set {KEY}.year_end_balances."
                                                      f"{y - 1} for the exact figure")
        amt = basis / f["factor"]
        rmd.update(f, basis_balance=basis, basis_source=basis_src, basis_exact=exact, amount=round(amt, 2),
                   remaining=round(max(0.0, amt - taken), 2), due=f"{y}-12-31")
        rmd["note"] = (f"{'Required' if rmd['status'] == 'required' else 'Applies only if the owner had started RMDs'}"
                       f". Prior Dec 31 balance / IRS Table I factor (age {f['beneficiary_age_first_year']} in {dy + 1}, "
                       "minus 1 each later year). Estimate; the custodian's RMD figure is authoritative. A missed RMD "
                       "costs a 25% excise tax (10% if corrected in time).")
    res["rmd"] = rmd
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args(argv)
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    r = report(con)
    if args.json:
        print(json.dumps(r, indent=1))
        return 0
    a = r.get("account") or {}
    print(f"{a.get('account_name', '?')}: ${a.get('balance') or 0:,.2f}")
    for m in r["missing"]:
        print("  ! " + m["msg"])
    if r.get("error"):
        print(r["error"])
    if "deadline" in r:
        d = r["deadline"]
        print(f"Deadline: {d['date']} ({d['years_left']} years after this one)")
    print(f"Taken in {r['year']}: ${r['this_year']['taken']:,.2f}")
    if "plan" in r:
        p = r["plan"]
        print(f"Even spread over {p['years']} years: ${p['flat_annual']:,.0f}/yr flat, "
              f"${p['growth_annual']:,.0f}/yr at {p['growth_rate']:.0%} growth")
    if "rmd" in r:
        print(f"RMD ({r['rmd']['status']}): " + (f"~${r['rmd']['amount']:,.0f}. " if "amount" in r["rmd"] else "")
              + r["rmd"].get("note", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
