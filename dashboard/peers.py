"""US benchmarks by age for the demo's "People your age" panel: family net worth (Federal Reserve Survey of Consumer
Finances) and household income (US Census Bureau, CPS ASEC), each in the age bands its source publishes. The page
does the lookup in the browser from PEERS (served inside the demo page as JSON); band_for() is the same lookup in
Python, for the tests.

Every figure was read from the official tables below; nothing here is estimated.
"""
from __future__ import annotations

# Net worth: SCF 2025, family net worth by age of the reference person, thousands of 2025 dollars.
#   Medians as published: "Changes in U.S. Family Finances from 2022 to 2025", Federal Reserve Bulletin (Oct 2026),
#   Table 2, https://www.federalreserve.gov/publications/files/scf26.pdf
#   25th/75th/90th percentiles: the Fed publishes none by age, so they are computed from the Fed's public SCF 2025
#   Summary Extract (https://www.federalreserve.gov/econres/scfindex.htm; NETWORTH by AGECL, weighted by WGT over all
#   five implicates). The same computation gives every published median back to within about $1,000.
#   The SCF's unit is the family (the economically dominant single person or couple and those financially
#   interdependent with them), not each person.
NET_WORTH = {
    "source": "Federal Reserve, Survey of Consumer Finances 2025",
    "url": "https://www.federalreserve.gov/publications/files/scf26.pdf",
    "year": 2025,
    "unit": "per family",
    "pct_note": "25th, 75th and 90th percentiles computed from the Fed's public SCF 2025 summary extract (weighted)",
    "bands": [  # [lowest age, highest age or None, p25, median, p75, p90] in dollars
        [0, 34, 3_100, 33_000, 145_100, 351_200],
        [35, 44, 18_000, 155_600, 467_500, 1_233_600],
        [45, 54, 54_700, 253_700, 942_000, 2_557_400],
        [55, 64, 71_800, 411_900, 1_416_200, 3_457_600],
        [65, 74, 118_100, 431_300, 1_312_100, 3_656_000],
        [75, None, 144_100, 504_900, 1_417_000, 4_188_300],
    ],
}

# Income: Census Bureau table HINC-02, "Age of Householder by Total Money Income in 2025", all races (2026 CPS ASEC;
#   report P60-289, "Income in the United States: 2025", Sept 2026), median household money income before taxes,
#   2025 dollars. https://www.census.gov/data/tables/time-series/demo/income-poverty/cps-hinc/hinc-02.html
#   Census publishes no under-35 median, only 15-24 and 25-34, so those are the bands here.
INCOME = {
    "source": "US Census Bureau, CPS ASEC 2026 (table HINC-02, income in 2025)",
    "url": "https://www.census.gov/data/tables/time-series/demo/income-poverty/cps-hinc/hinc-02.html",
    "year": 2025,
    "unit": "per household",
    "bands": [  # [lowest age, highest age or None, median]
        [15, 24, 60_870],
        [25, 34, 94_880],
        [35, 44, 114_100],
        [45, 54, 120_100],
        [55, 64, 99_320],
        [65, 74, 68_800],
        [75, None, 50_880],
    ],
}

# For the demo person's own pay: Census table PINC-03 (2026 CPS ASEC, earnings in 2025), men 35-44 who worked
# full-time, year-round: median earnings $81,560.
# https://www.census.gov/data/tables/time-series/demo/income-poverty/cps-pinc/pinc-03.html
MEN_35_44_FULL_TIME_EARNINGS = 81_560

PEERS = {"net_worth": NET_WORTH, "income": INCOME}


def band_for(table: dict, age: int | float) -> list | None:
    """The row of `table` (NET_WORTH or INCOME) whose ages hold `age`, or None outside them all."""
    for row in table["bands"]:
        lo, hi = row[0], row[1]
        if age >= lo and (hi is None or age < hi + 1):
            return row
    return None


def band_label(row: list) -> str:
    if row[1] is None:
        return f"{row[0]}+"
    return f"under {row[1] + 1}" if row[0] == 0 else f"{row[0]}-{row[1]}"
