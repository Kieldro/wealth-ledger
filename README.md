# card-tracker

Personal-finance tracker that runs on your own machine. It pulls bank, card and brokerage data from
[SimpleFIN Bridge](https://beta-bridge.simplefin.org/) and [Plaid](https://plaid.com/) into one local SQLite
database, classifies transactions, values the things no bank feed covers (precious metals, crypto wallets, home,
vehicles) from free public sources, works out net worth against a Google Sheet balance sheet, and serves a
private read-only dashboard.

No personal data is in this repo. Account labels, sheet id, rules that name your own banks, home and vehicle
details all live in `~/.config/card-tracker/personal.json`, outside the repo.

## Layout

| Path | What it does |
|---|---|
| `ctdb.py` | Shared SQLite schema and views (`v_ledger`, `v_accounts`, ...) and the generic classification rules |
| `personal.py` | Loads `~/.config/card-tracker/personal.json` (or `$CARD_TRACKER_PERSONAL`) |
| `personal.example.json` | Template for `personal.json`, with placeholder values |
| `simplefin/simplefin_sync.py` | SimpleFIN Bridge -> SQLite (transactions, balances, holdings) |
| `plaid/plaid_sync.py` | Plaid -> SQLite, for accounts SimpleFIN can't reach (link / relink / sync) |
| `networth.py` | Net worth = live synced balances + the sheet's manual rows + valuations |
| `valuations/` | Daily/monthly estimates: `metals`, `crypto`, `home`, `cars`; `run.py` runs whichever are due |
| `dashboard/` | Private dashboard (stdlib HTTP server + one HTML page), see `dashboard/README.md` |
| `bin/card-tracker-sync` | Daily cron job: SimpleFIN + Plaid sync, then valuations; writes a status file, never notifies |
| `bin/simplefin-institution-check` | Monthly check whether an institution you are waiting for is in SimpleFIN yet |
| `scraper/` | Experimental Playwright bank-scraper base class, tested against a local mock bank |
| `tiller/` | Experimental Tiller Money Google Sheet -> SQLite sync |

Data lives outside the repo too: the database is `~/.local/share/card-tracker/txns.db`, and API credentials
(`simplefin_access_url`, `plaid.json`, `crypto_wallets.json`, `dashboard_auth.json`) are in
`~/.config/card-tracker/` with mode 0600.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install requests playwright pytest keyring google-api-python-client google-auth
.venv/bin/playwright install chromium          # home/cars valuations and the scraper

mkdir -p ~/.config/card-tracker && chmod 700 ~/.config/card-tracker
cp personal.example.json ~/.config/card-tracker/personal.json
chmod 600 ~/.config/card-tracker/personal.json  # then replace every placeholder with your own values

.venv/bin/python simplefin/simplefin_sync.py claim <setup-token>   # or: bin/card-tracker-sync claim <token>
.venv/bin/python plaid/plaid_sync.py setup <client_id> <secret>    # optional
.venv/bin/python dashboard/server.py set-password --generate      # dashboard login
```

`networth.py` and the metals/crypto valuations read the sheet with the [`gws`](https://github.com/googleworkspace/cli)
CLI. Example cron entries (symlink the `bin/` scripts into `~/.local/bin` first):

```cron
15 8 * * * ~/.local/bin/card-tracker-sync >> ~/.cache/card-tracker-sync.log 2>&1
0 9 1 * *  ~/.local/bin/simplefin-institution-check >> ~/.cache/simplefin-institution-check.log 2>&1
```

## Tests

```sh
.venv/bin/python -m pytest -q
```

The tests use made-up data and `personal.example.json` (see `conftest.py`), never your real `personal.json`.
