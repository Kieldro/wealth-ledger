# wealth-ledger dashboard

Private, read-only finance dashboard: net worth, allocation, trend, accounts, holdings, cards, spending.

- **URLs:** `http://127.0.0.1:8766/` and `http://<tailscale-ip>:8766/` (the machine's Tailscale IPv4, so a phone on the tailnet can reach it). It binds only these two addresses, not the LAN or Docker bridges. `--no-tailscale` binds loopback only.
- **Service:** a systemd user unit, e.g. `~/.config/systemd/user/wealth-ledger-dashboard.service` running `.venv/bin/python dashboard/server.py --port 8766` with `Restart=on-failure`. Logs: `journalctl --user -u wealth-ledger-dashboard`.
- **Files:** `server.py` (stdlib http.server + JSON API + login), `index.html` (one page, Chart.js from cdnjs with SRI; no data leaves the machine).

## Auth
Every path needs a login, including requests from 127.0.0.1 (a Cloudflare Tunnel would connect from localhost). Only `/login`, `POST /logout` and `/robots.txt` are public.
- **Login:** `/login`. Success sets an HMAC-signed `ctd_session` cookie (HttpOnly, SameSite=Lax, 365 days, sliding: opening the page after a day re-issues it for another 365, so a device in use never gets logged out; `Secure` when the request came over HTTPS via `X-Forwarded-Proto`/`Cf-Visitor`). Sessions are also tracked server-side, so **Log out** really revokes the cookie. Unauthenticated pages redirect to `/login`; `/api/*` and `/healthz` return 401.
- **Credentials:** `~/.config/wealth-ledger/dashboard_auth.json` (must be 0600, else every login is refused): username, scrypt hash (N=2^17, r=8, p=1, random salt) and a random session-signing secret. Active sessions: `dashboard_sessions.json` in the same dir.
- **Set or rotate the password** (signs out every device; the running service picks it up, no restart):
  `.venv/bin/python dashboard/server.py set-password [--username NAME]` (prompts), or add `--generate` to print a random 24-letter passphrase once. Without `--username` it keeps the current one, else uses `dashboard.default_username` from personal.json.
- **Brute force:** 5 failures per client IP in 15 min, then 429 until the window clears; 50 failures in an hour from anywhere locks all logins for 15 min. Client IP = `Cf-Connecting-IP` when the request comes from loopback (cloudflared), else the socket peer. Limits live in memory: restarting the service clears a lockout (sessions survive restarts). Failed logins are logged with IP (never passwords): `journalctl --user -u wealth-ledger-dashboard | grep auth:`.
- **Headers/CSRF:** CSP (scripts limited to the page's inline-script hash + the pinned cdnjs Chart.js), `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `nosniff`, `Cache-Control: no-store`, `X-Robots-Tag: noindex`, HSTS over HTTPS. Refresh (`POST /api/refresh`) and logout are POST-only, and cross-site POSTs (`Sec-Fetch-Site`) are refused.

## Data
- `~/.local/share/wealth-ledger/txns.db`, opened `mode=ro`. The dashboard never writes to it and never runs a sync.
- `networth.py --json` (reads the balance-sheet tab of the Google Sheet in personal.json via `gws`), cached 10 min. **Refresh** (`POST /api/refresh`) re-reads the sheet only; it does not call SimpleFIN or Plaid.
- `~/.cache/wealth-ledger-status` for the last sync time and problems.

## API
`/api/summary`, `POST /api/refresh`, `/api/trend`, `/api/accounts`, `/api/holdings`, `/api/cards`, `/api/spending`, `/healthz`.

## Notes
- Categories map networth.py lines using `dashboard.exact` / `dashboard.rules` in personal.json (generic regex fallbacks in `server.py`); unknown lines fall into "Other". Sheet values are only as fresh as the last manual update and are badged `sheet`.
- Trend = daily synced snapshots (same accounts networth.py counts: no custodial accounts, crypto exchange wallets or superseded feeds) + today's sheet-only total held constant. 1/7/30-day changes show n/a until that much history exists. Before the first daily snapshot (`first_daily`), `/api/trend` also returns `sheet_points`: whole net worth (Total Equity) per dated column of the yearly balance-sheet spreadsheets, stored weekly by `insights/nw_history.py`.
- `/api/trend` also returns `prices` (`{"BTC"|"XAU": {date: usd}}`, every series and sheet_points date, that day's close or the nearest earlier one; XAU is USD per troy oz) and `spot` (`{"BTC"|"XAU": {usd, date, source}}` or null, the latest price: the `prices` table, or the valuations providers' live spot when newer), from `insights/prices.py`, for net worth in bitcoin and gold.
- Spending = `class='spend'`, posted, since `dashboard.ledger_start`. Merchant names are lightly normalized from payee/description.
- Holdings exclude custodial accounts and blank-symbol sweeps. SimpleFIN has no cost basis.
- Port change: edit `--port` in the unit, then `systemctl --user daemon-reload && systemctl --user restart wealth-ledger-dashboard`.
