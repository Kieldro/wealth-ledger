"""Tiny fake bank for testing the scraper: login -> OTP -> activity -> Download CSV.

Behaviours that mimic real banks:
  * session cookie (server-side session id)
  * "remember this device" cookie set after a correct OTP -> later logins skip OTP
  * the "SMS" is written to <sms_file> (a test harness relays it, like the SMS gateway)
  * /admin/expire_sessions drops sessions only (device stays trusted)
Run standalone:  python -m mockbank.server   (from scraper/)
"""
from __future__ import annotations

import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

USER, PASSWORD = "demo", "hunter2"

CSV = (
    "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
    "10/14/2026,10/15/2026,BLUE BOTTLE COFFEE,Food & Drink,Sale,-5.75,\n"
    "10/14/2026,10/15/2026,BLUE BOTTLE COFFEE,Food & Drink,Sale,-5.75,\n"
    "10/13/2026,10/14/2026,AMAZON MKTPLACE,Shopping,Sale,-42.10,\n"
    "10/12/2026,10/12/2026,PAYMENT THANK YOU,,Payment,500.00,\n"
)

PAGE = "<!doctype html><html><body>{}</body></html>"


class MockBank:
    def __init__(self, sms_file: Path, host="127.0.0.1", port=0):
        self.sms_file = Path(sms_file)
        self.sessions: set[str] = set()
        self.pending: dict[str, str] = {}   # pending-login token -> otp code
        self.devices: set[str] = set()
        self.stats = {"logins": 0, "otps": 0, "downloads": 0}
        bank = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            # helpers
            def cookies(self):
                c = SimpleCookie(self.headers.get("Cookie", ""))
                return {k: v.value for k, v in c.items()}

            def send(self, body, code=200, ctype="text/html", headers=()):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def redirect(self, to, headers=()):
                self.send_response(302)
                self.send_header("Location", to)
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()

            def start_session(self):
                sid = secrets.token_hex(8)
                bank.sessions.add(sid)
                return ("Set-Cookie", f"mb_sess={sid}; Path=/; Max-Age=3600")

            def form(self):
                n = int(self.headers.get("Content-Length", 0))
                return {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}

            # routes
            def do_GET(self):
                ck = self.cookies()
                path = self.path.split("?")[0]
                if path == "/login":
                    return self.send(PAGE.format(
                        '<h1>MockBank</h1><form method=post action=/login>'
                        '<input id=user name=user><input id=pass name=pass type=password>'
                        '<button id=signin type=submit>Sign in</button></form>'))
                if path == "/otp":
                    return self.send(PAGE.format(
                        '<h1>Verify</h1><p id=otp-msg>We texted you a code.</p>'
                        '<form method=post action=/otp><input id=otp name=otp>'
                        '<button id=otp-submit type=submit>Verify</button></form>'))
                if path == "/activity":
                    if ck.get("mb_sess") not in bank.sessions:
                        return self.redirect("/login")
                    return self.send(PAGE.format(
                        '<h1 id=acct>Activity</h1><table><tr><td>coffee</td></tr></table>'
                        '<a id=download-csv href="/download.csv">Download CSV</a>'))
                if path == "/download.csv":
                    if ck.get("mb_sess") not in bank.sessions:
                        return self.redirect("/login")
                    bank.stats["downloads"] += 1
                    return self.send(CSV, ctype="text/csv", headers=[
                        ("Content-Disposition", 'attachment; filename="Chase_Activity.CSV"')])
                if path == "/admin/expire_sessions":
                    bank.sessions.clear()
                    return self.send("ok")
                self.send("not found", 404)

            def do_POST(self):
                ck = self.cookies()
                f = self.form()
                if self.path == "/login":
                    if f.get("user") != USER or f.get("pass") != PASSWORD:
                        return self.send(PAGE.format("<p id=err>Bad credentials</p>"), 401)
                    bank.stats["logins"] += 1
                    if ck.get("mb_device") in bank.devices:
                        return self.redirect("/activity", [self.start_session()])
                    tok, code = secrets.token_hex(8), f"{secrets.randbelow(10**6):06d}"
                    bank.pending[tok] = code
                    bank.sms_file.write_text(f"MockBank: your verification code is {code}. Do not share it.")
                    return self.redirect("/otp", [("Set-Cookie", f"mb_pending={tok}; Path=/")])
                if self.path == "/otp":
                    tok = ck.get("mb_pending", "")
                    if bank.pending.get(tok) and f.get("otp") == bank.pending[tok]:
                        bank.stats["otps"] += 1
                        dev = secrets.token_hex(8)
                        bank.devices.add(dev)
                        return self.redirect("/activity", [
                            self.start_session(),
                            ("Set-Cookie", f"mb_device={dev}; Path=/; Max-Age=31536000")])
                    return self.send(PAGE.format("<p id=err>Wrong code</p>"), 401)
                self.send("not found", 404)

        self.httpd = ThreadingHTTPServer((host, port), H)
        self.url = f"http://{host}:{self.httpd.server_address[1]}"
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    b = MockBank(Path("/tmp/mockbank_sms.txt")).start()
    print("MockBank at", b.url, f"(login {USER}/{PASSWORD}); Ctrl-C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        b.stop()
