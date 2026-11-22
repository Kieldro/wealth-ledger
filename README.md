# wealth-ledger

Personal-finance tracker that runs on your own machine. It pulls bank, card and brokerage data from
[SimpleFIN Bridge](https://beta-bridge.simplefin.org/) and [Plaid](https://plaid.com/) into one local SQLite
database, classifies transactions, values the things no bank feed covers (precious metals, crypto wallets, home,
vehicles) from free public sources, works out net worth against a Google Sheet balance sheet, and serves a
private read-only dashboard.

No personal data is in this repo. Account labels, sheet id, rules that name your own banks, home and vehicle
details all live in `~/.config/wealth-ledger/personal.json`, outside the repo.

## Quick start (SimpleFIN only)

A [SimpleFIN Bridge](https://beta-bridge.simplefin.org/) account (a small paid service) is all you
need. Everything else on this page is an optional add-on; without one, its step is skipped and its dashboard
card is hidden.

Requirements: Linux with `bash`, `flock` (util-linux, installed by default) and Python 3.10 or newer.

```sh
git clone <this repo> wealth-ledger && cd wealth-ledger
python3 -m venv .venv
.venv/bin/pip install -r simplefin/requirements.txt       # just "requests"

# 1. In SimpleFIN Bridge, connect your banks, then create a setup token for a new app.
# 2. Claim it (works once) and pull the first 90 days:
bin/wealth-ledger-sync claim <setup-token>

# 3. Set the dashboard login (prints a passphrase once) and start it:
.venv/bin/python dashboard/server.py set-password --username me --generate
.venv/bin/python dashboard/server.py                      # http://127.0.0.1:8766/
```

Then sync once a day from cron (`crontab -e`; SimpleFIN allows about 24 requests a day, and one sync is one
request):

```cron
15 8 * * * /path/to/wealth-ledger/bin/wealth-ledger-sync >> ~/.cache/wealth-ledger-sync.log 2>&1
```

What you get: accounts and balances, holdings, credit cards, transactions classified into spending, income,
transfers and card payments, monthly spending and income, subscriptions and unusual charges, upcoming drafts, net
worth from your synced accounts, and its trend with S&P 500, Nasdaq, gold and bitcoin overlays (public prices, no
API key).

- **Account types.** Each account's type (checking, savings, credit, investment, retirement, hsa, loan) is guessed
  from its name. One that can't be guessed is left out of the totals and named in the page's status line; give it
  a type in `personal.json` and it is counted from the next sync:
  `{"account_kinds": {"Example Unnamed Account%": "savings"}}`
- **Files.** The access URL is `~/.config/wealth-ledger/simplefin_access_url` (a secret, mode 0600), the database
  `~/.local/share/wealth-ledger/txns.db`, the last sync's result `~/.cache/wealth-ledger-status`. `personal.json`
  is optional; `personal.example.json` lists every key.
- **Somewhere else.** `WEALTH_LEDGER_CONFIG_DIR`, `WEALTH_LEDGER_DB` and `WEALTH_LEDGER_STATUS` move those three
  (set them for both the sync and the dashboard); `WEALTH_LEDGER_PYTHON` names another Python, and
  `WEALTH_LEDGER_PATH` adds folders to the sync's `PATH` under cron.
- **Sync on demand.** The dashboard's **Sync now** button runs the same sync at once (not within 15 minutes of
  the last one, at most 8 times a day, to stay inside SimpleFIN's allowance); see `dashboard/README.md`.
- **Keep it running.** A systemd user unit for the dashboard is in `dashboard/README.md`. It listens on
  127.0.0.1 only (plus the machine's Tailscale address when Tailscale is installed; `--no-tailscale` turns that
  off).

### Optional add-ons

Each is off until you set it up, and none is needed by the others unless noted.

| Add-on | What it adds | Turn it on |
|---|---|---|
| Plaid | Accounts SimpleFIN can't reach, card credit limits | `plaid/plaid_sync.py setup <client_id> <secret>`, then `link` |
| Google Sheet | Assets no feed sees (cash, metals, home, cars) in net worth, and a budget | `sheet_id` + `networth` in `personal.json`; needs the [`gws`](https://github.com/googleworkspace/cli) CLI |
| Metals, crypto wallets | Daily values for the sheet's metals and crypto rows | Google Sheet; `crypto_wallets.json` for wallets |
| Home and car values | Monthly estimates from public listings | Google Sheet; `home` / `cars` in `personal.json`; Playwright |
| Zelle memos | The note typed on each Zelle, from your bank's emails | Google Sheet; `gws` signed in to Gmail |
| Monthly email | Last month's summary, mailed to yourself | `monthly_email.enabled` in `personal.json`; `gws` signed in to Gmail |
| Rental property | Income and expenses of a rental | `rental` in `personal.json` |
| Inherited IRA | Required-distribution planning | `inherited_ira` in `personal.json` |
| Card limits | Utilization for cards whose feed has no limit | `credit_limits` in `personal.json` |
| Own rules | Fix how a transaction is classified | `rules` in `personal.json` |
| Backups | Daily gzipped copy of the database to another disk | `backup.dir` in `personal.json` |
| Institution watch | Tells you when a bank you are waiting for joins SimpleFIN | `simplefin_watch` in `personal.json` + a monthly cron line |
| Amazon orders | Splits Amazon charges across the items bought | Google Sheet budget; `insights/amazon.py` import |
| Second ledger | Another person's accounts on another port | see the last section |

## Layout

| Path | What it does |
|---|---|
| `ctdb.py` | Shared SQLite schema and views (`v_ledger`, `v_accounts`, ...) and the generic classification rules. `v_accounts.credit_limit` is the card limit from the feed, else from `credit_limits` in `personal.json` (SimpleFIN reports none); `credit_limit_source` says which |
| `personal.py` | Loads `~/.config/wealth-ledger/personal.json` (or `$WEALTH_LEDGER_PERSONAL`); the file is optional |
| `personal.example.json` | Every `personal.json` key with placeholder values; all optional |
| `simplefin/simplefin_sync.py` | SimpleFIN Bridge -> SQLite (transactions, balances, holdings) |
| `plaid/plaid_sync.py` | Plaid -> SQLite, for accounts SimpleFIN can't reach (link / relink / sync); also stores card credit limits (`balances.limit`) |
| `fidelity/history_import.py` | One-off backfill of Fidelity history from saved fidelity.com activity responses (JSON, kept outside the repo) into `txns` as source `fidelity_web`: only rows dated before each account's first live-feed transaction, on the live account's id, with the overlap compared against the feed; idempotent, `--dry-run` to preview |
| `networth.py` | Net worth = live synced balances + the sheet's manual rows + valuations |
| `sheet_marks.py` | Marks balance-sheet rows nothing keeps current (yellow label + note); unmarks them once they're live. Formatting only |
| `sheet_estimates.py` | Writes the latest home valuation, its date and the estimates behind it to the sheet's `estimates` tab (only that tab; only when it changed), so the balance sheet can point at it |
| `valuations/` | Daily/monthly estimates: `metals`, `crypto`, `home`, `cars`; `run.py` runs whichever are due |
| `insights/overdraft.py` | Overdraft check: projects recurring bills, card autopays and pay 31 days ahead per checking account |
| `insights/zelle_memos.py` | Adds the note typed on each Zelle (from the bank's Zelle emails, read with `gws`) to the matching transaction's memo |
| `insights/amazon.py` | Imports an Amazon "Your Orders" data export (items, refunds; no addresses/gift/tracking), matches Amazon card charges to the shipments they paid for, so `insights/budget.py` splits each charge across its items' budget lines |
| `insights/monthly_email.py` | Opt-in monthly summary email built from the ledger database alone, no spreadsheet (income, spending by category vs the month before, balances, rental, unusual charges), sent to yourself with `gws` once a month by the daily sync; without `--send` it only prints |
| `insights/nw_history.py` | Past net-worth points (Total Equity per dated column) from the yearly balance-sheet spreadsheets, read with `gws`, for the dashboard trend; weekly |
| `insights/prices.py` | Daily closes into `prices` (keyless): BTC (Coinbase candles, Kraken fallback), gold/silver/platinum/copper (Yahoo COMEX futures; Coinbase PAXG fallback for gold), rial and dinar (latest rate only) so the dashboard can show every value in them, plus the S&P 500 and Nasdaq (Yahoo ^GSPC, ^IXIC) for the trend's benchmark overlays; backfills once, then 1 request per symbol daily. Also runs `insights/holding_prices.py` |
| `insights/live_markets.py` | Live Bitcoin (Coinbase, CoinGecko), gold (Swissquote spot, via `valuations/metals.py`) and S&P 500 (Yahoo ^GSPC) for the signed-in dashboard's markets line only, each fetched at most once a minute; nothing else is priced live |
| `insights/holding_prices.py` | Daily closes of every stock, ETF and fund held in a counted account into `security_prices` (Yahoo, keyless; money market is $1 and never fetched), so the trend values holdings at each day's close instead of the feed's late balances; 1 request per held symbol daily |
| `dashboard/` | Private dashboard (stdlib HTTP server + one HTML page), see `dashboard/README.md` |
| `bin/wealth-ledger-sync` | Daily cron job: SimpleFIN sync, then whichever add-ons are set up (Plaid, valuations, backup, ...), prices and the overdraft check; writes a status file, never notifies (the opt-in monthly email aside) |
| `bin/wealth-ledger-backup` | Daily integrity-checked, gzipped copy of the database to another disk (`backup` in `personal.json`); run by the sync |
| `bin/simplefin-institution-check` | Monthly check whether an institution you are waiting for is in SimpleFIN yet |
| `scraper/` | Experimental Playwright bank-scraper base class, tested against a local mock bank |
| `tiller/` | Experimental Tiller Money Google Sheet -> SQLite sync |

Data lives outside the repo too: the database is `~/.local/share/wealth-ledger/txns.db`, and API credentials
(`simplefin_access_url`, `plaid.json`, `crypto_wallets.json`, `dashboard_auth.json`) are in
`~/.config/wealth-ledger/` with mode 0600.

## Setup with the add-ons

The quick start above is enough for a ledger of synced accounts. For the spreadsheet, valuations and the scraper:

```sh
python3 -m venv .venv
.venv/bin/pip install requests playwright pytest keyring google-api-python-client google-auth
.venv/bin/playwright install chromium          # home/cars valuations, the scraper, and the dashboard's browser tests

mkdir -p ~/.config/wealth-ledger && chmod 700 ~/.config/wealth-ledger
cp personal.example.json ~/.config/wealth-ledger/personal.json
chmod 600 ~/.config/wealth-ledger/personal.json  # keep the sections you use, with your own values; delete the rest

.venv/bin/python simplefin/simplefin_sync.py claim <setup-token>   # or: bin/wealth-ledger-sync claim <token>
.venv/bin/python plaid/plaid_sync.py setup <client_id> <secret>    # optional
.venv/bin/python dashboard/server.py set-password --generate      # dashboard login
```

`networth.py` and the metals/crypto valuations read the sheet with the [`gws`](https://github.com/googleworkspace/cli)
CLI. Example cron entries (symlink the `bin/` scripts into `~/.local/bin` first):

```cron
15 8 * * * ~/.local/bin/wealth-ledger-sync >> ~/.cache/wealth-ledger-sync.log 2>&1
0 9 1 * *  ~/.local/bin/simplefin-institution-check >> ~/.cache/simplefin-institution-check.log 2>&1
```

## Tests

```sh
.venv/bin/python -m pytest -q             # everything
.venv/bin/python -m pytest -q -m ui       # only the dashboard's browser tests (Playwright + Chromium; skipped without them)
.venv/bin/python -m pytest -q -m "not ui" # everything else
```

The tests use made-up data and `personal.example.json` (see `conftest.py`), never your real `personal.json`. The
browser tests run the dashboard page against a throwaway ledger: see [dashboard/README.md](dashboard/README.md#browser-tests).

## A second ledger (another person, no spreadsheet)

Every path can be overridden, so one checkout can serve a separate ledger for someone else: set
`WEALTH_LEDGER_CONFIG_DIR`, `WEALTH_LEDGER_PERSONAL`, `WEALTH_LEDGER_DB`, `WEALTH_LEDGER_STATUS`, `WEALTH_LEDGER_LOCK`,
`WEALTH_LEDGER_EMAIL_STATE`, `WEALTH_LEDGER_SYNC_LOG` and `PLAID_CONFIG` for its sync (cron) and its dashboard
(another port); that dashboard's Sync now button then syncs that ledger.

A ledger with no `personal.json`, or one without `sheet_id`, runs on synced accounts alone: the sync skips every step that reads a
spreadsheet, Gmail or wallets, and the dashboard hides the sections that need them. Useful keys for such a ledger:

- `"auto_kind"`: account types (checking, savings, credit, retirement...) are guessed from account names at
  each sync (the default without `sheet_id`; `false` turns it off); an account that can't be guessed is reported
  as a problem and left out of the totals until `account_kinds` names its type.
- `"dashboard": {"web_connect": true}`: while nothing is connected, the page shows how to link banks in SimpleFIN
  and takes the setup token; it claims it and runs the first 90-day sync. An existing connection can't be replaced
  from the page.

## License

Copyright (C) 2026 Kieldro

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
Public License as published by the Free Software Foundation, version 3. It is distributed in the hope that it will be
useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR
PURPOSE. See [LICENSE](LICENSE) for the full text. If you run a modified version as a network service, the AGPL
requires you to offer its users the modified source.

One file is someone else's: `dashboard/testdata/chart-4.4.1.umd.min.js` is an unmodified copy of Chart.js, used only
by the browser tests, under the MIT license in the `.LICENSE.txt` file beside it.
