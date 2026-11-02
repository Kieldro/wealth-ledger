# wealth-ledger

Personal-finance tracker that runs on your own machine. It pulls bank, card and brokerage data from
[SimpleFIN Bridge](https://beta-bridge.simplefin.org/) and [Plaid](https://plaid.com/) into one local SQLite
database, classifies transactions, values the things no bank feed covers (precious metals, crypto wallets, home,
vehicles) from free public sources, works out net worth against a Google Sheet balance sheet, and serves a
private read-only dashboard.

No personal data is in this repo. Account labels, sheet id, rules that name your own banks, home and vehicle
details all live in `~/.config/wealth-ledger/personal.json`, outside the repo.

## Layout

| Path | What it does |
|---|---|
| `ctdb.py` | Shared SQLite schema and views (`v_ledger`, `v_accounts`, ...) and the generic classification rules. `v_accounts.credit_limit` is the card limit from the feed, else from `credit_limits` in `personal.json` (SimpleFIN reports none); `credit_limit_source` says which |
| `personal.py` | Loads `~/.config/wealth-ledger/personal.json` (or `$WEALTH_LEDGER_PERSONAL`) |
| `personal.example.json` | Template for `personal.json`, with placeholder values |
| `simplefin/simplefin_sync.py` | SimpleFIN Bridge -> SQLite (transactions, balances, holdings) |
| `plaid/plaid_sync.py` | Plaid -> SQLite, for accounts SimpleFIN can't reach (link / relink / sync); also stores card credit limits (`balances.limit`) |
| `networth.py` | Net worth = live synced balances + the sheet's manual rows + valuations |
| `sheet_marks.py` | Marks balance-sheet rows nothing keeps current (yellow label + note); unmarks them once they're live. Formatting only |
| `valuations/` | Daily/monthly estimates: `metals`, `crypto`, `home`, `cars`; `run.py` runs whichever are due |
| `insights/overdraft.py` | Overdraft check: projects recurring bills, card autopays and pay 31 days ahead per checking account |
| `insights/zelle_memos.py` | Adds the note typed on each Zelle (from the bank's Zelle emails, read with `gws`) to the matching transaction's memo |
| `insights/amazon.py` | Imports an Amazon "Your Orders" data export (items, refunds; no addresses/gift/tracking), matches Amazon card charges to the shipments they paid for, so `insights/budget.py` splits each charge across its items' budget lines |
| `insights/nw_history.py` | Past net-worth points (Total Equity per dated column) from the yearly balance-sheet spreadsheets, read with `gws`, for the dashboard trend; weekly |
| `insights/prices.py` | Daily closes into `prices` (keyless): BTC (Coinbase candles, Kraken fallback), gold/silver/platinum/copper (Yahoo COMEX futures; Coinbase PAXG fallback for gold), rial and dinar (latest rate only) so the dashboard can show every value in them, plus the S&P 500 and Nasdaq (Yahoo ^GSPC, ^IXIC) for the trend's benchmark overlays; backfills once, then 1 request per symbol daily |
| `dashboard/` | Private dashboard (stdlib HTTP server + one HTML page), see `dashboard/README.md` |
| `bin/wealth-ledger-sync` | Daily cron job: SimpleFIN + Plaid sync, valuations, overdraft check, backup; writes a status file, never notifies |
| `bin/wealth-ledger-backup` | Daily integrity-checked, gzipped copy of the database to another disk (`backup` in `personal.json`); run by the sync |
| `bin/simplefin-institution-check` | Monthly check whether an institution you are waiting for is in SimpleFIN yet |
| `scraper/` | Experimental Playwright bank-scraper base class, tested against a local mock bank |
| `tiller/` | Experimental Tiller Money Google Sheet -> SQLite sync |

Data lives outside the repo too: the database is `~/.local/share/wealth-ledger/txns.db`, and API credentials
(`simplefin_access_url`, `plaid.json`, `crypto_wallets.json`, `dashboard_auth.json`) are in
`~/.config/wealth-ledger/` with mode 0600.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install requests playwright pytest keyring google-api-python-client google-auth
.venv/bin/playwright install chromium          # home/cars valuations and the scraper

mkdir -p ~/.config/wealth-ledger && chmod 700 ~/.config/wealth-ledger
cp personal.example.json ~/.config/wealth-ledger/personal.json
chmod 600 ~/.config/wealth-ledger/personal.json  # then replace every placeholder with your own values

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
.venv/bin/python -m pytest -q
```

The tests use made-up data and `personal.example.json` (see `conftest.py`), never your real `personal.json`.
