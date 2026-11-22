"""Browser tests of the dashboard page: headless Chromium (Playwright) against a throwaway dashboard on made-up data
(ui_fixture.py). Nothing here reads the owner's ledger, config or spreadsheet, and no request leaves the machine.

    .venv/bin/python -m pytest -m ui             # only these
    .venv/bin/python -m pytest -m "not ui"       # everything else

They skip themselves when Playwright or its Chromium isn't installed. Every test also fails on an uncaught page error,
a console error or warning, a failed request, an HTTP error or a request to another host, unless the test said to
expect it (page.watch.expect). A failing test leaves a screenshot and the page's console/network log in
dashboard/ui-artifacts/ (git-ignored; synthetic data only) and prints where.

Waiting: expect(...) retries until the page gets there, a slow answer is a request the test holds and then lets go,
and the page's own clocks (the 20-second give-up, the 2-second sync poll) are moved with page.clock, so nothing sleeps.
"""
import base64
import fcntl
import hashlib
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api", reason="Playwright isn't installed (.venv/bin/pip install playwright)")
from playwright.sync_api import Error, expect, sync_playwright  # noqa: E402

from dashboard import ui_fixture as F  # noqa: E402

pytestmark = pytest.mark.ui

HERE = Path(__file__).resolve().parent
ARTIFACTS = Path(os.environ.get("WEALTH_LEDGER_UI_ARTIFACTS") or HERE / "ui-artifacts")
CHART_URL = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"  # the page's one outside file
CHART_JS = HERE / "testdata" / "chart-4.4.1.umd.min.js"  # served to the page in its place
DESK, PHONE = {"width": 1280, "height": 900}, {"width": 390, "height": 844}
NOT_LOCAL = re.compile(r"^https?://(?!127\.0\.0\.1[:/])")
READY = 'body[data-ready="1"]'  # set when the page's first load has finished, whatever came of it
CARDS = ".grid > section.card"
WAITING = "[data-load]"  # a card (or the totals) that is loading, failed, or showing saved numbers
API = ("summary", "trend", "accounts", "holdings", "crypto", "cards", "spending", "drafts", "budget", "subscriptions",
       "rental", "inherited_ira", "last", "sync", "account_txns?id=chk")
SHEET_ONLY = {"budget", "cryptocard", "ira", "rentalcard"}  # cards a ledger with no personal.json has nothing for
CHARTS = ("donut", "liabdonut", "trend", "spend", "pace", "rentalchart", "iratrend")
# something each card can only say once its data is in
CARD_SAYS = {
    "draftscard": "EXAMPLE EMPLOYER PAYROLL", "alloccard": "Cash & bank", "trendcard": "daily snapshots",
    "liabcard": "Total owed", "cardscard": "Example Rewards Card", "acctscard": "Example Bank Checking",
    "holdscard": "Total Market Fund", "spendcard": "Income vs spending", "txcard": "Example Coffee Roasters",
    "budget": "Groceries", "subscard": re.compile("streamco", re.I), "cryptocard": "Cold Wallet",
    "ira": "Must be empty by", "rentalcard": "Last 3 months",
}
NET_WORTH, ASSETS, LIABILITIES = (f"${n:,.0f}" for n in (F.ASSETS - F.LIABILITIES, F.ASSETS, F.LIABILITIES))

expect.set_options(timeout=10_000)


# ---------- the browser, the two dashboards ----------
@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Error as e:
            pytest.skip("Playwright's Chromium isn't installed (.venv/bin/playwright install chromium): "
                        + str(e).strip().splitlines()[0])
        yield b
        b.close()


def outside(route, seen: list):
    """A request to another host: Chart.js comes from the copy in testdata, anything else is refused and noted."""
    if route.request.url == CHART_URL:
        return route.fulfill(status=200, body=CHART_JS.read_bytes(), content_type="application/javascript",
                             headers={"Access-Control-Allow-Origin": "*"})
    seen.append(route.request.url)
    route.abort()


def sign_in(page, inst, password=None):
    page.goto(inst.url + "/login")
    page.fill("#u", F.USER)
    page.fill("#p", inst.password if password is None else password)
    page.click("#go")


def instance(browser, sheet: bool):
    """A running dashboard on synthetic data, with a signed-in session the tests' pages start from."""
    folder = Path(tempfile.mkdtemp(prefix="wealth-ledger-ui-"))
    inst = F.Instance(folder, sheet=sheet)
    try:
        inst.start()
        ctx = browser.new_context(viewport=DESK)
        ctx.route(NOT_LOCAL, lambda route: outside(route, []))
        page = ctx.new_page()
        sign_in(page, inst)
        page.wait_for_selector(READY)  # a full load, so the dashboard has its "last good" copy of every answer
        inst.state = {"cookies": ctx.storage_state()["cookies"], "origins": []}  # the session only: no remembered settings
        ctx.close()
        yield inst
    finally:
        inst.stop()
        shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(scope="module")
def full(browser):
    """The whole dashboard: personal.json, a spreadsheet (canned rows), every card."""
    yield from instance(browser, sheet=True)


@pytest.fixture(scope="module")
def sfo(browser):
    """SimpleFIN-only: no personal.json, no spreadsheet."""
    yield from instance(browser, sheet=False)


@pytest.fixture(autouse=True)
def fresh(request):
    """Each test starts from a clean sync two hours ago, with no Sync now history."""
    for name in ("full", "sfo"):
        if name in request.fixturenames:
            request.getfixturevalue(name).reset()


# ---------- pages that are watched ----------
class Watch:
    """Everything that went wrong on a page. Tests that make something fail say what to expect."""

    def __init__(self):
        self.events, self.found, self.holds, self.expected = [], [], [], []

    def listen(self, page):
        def console(m):
            self.events.append(f"console.{m.type}: {m.text}")
            if m.type in ("error", "warning"):
                self.found.append(("console", f"console.{m.type}: {m.text} [{m.location.get('url', '')}]"))

        def failed(r):
            self.events.append(f"request failed: {r.method} {r.url} ({r.failure})")
            if "ERR_ABORTED" not in (r.failure or ""):  # ERR_ABORTED: dropped by a reload or by the page giving up
                self.found.append(("request", f"request failed: {r.method} {r.url} ({r.failure})"))

        def response(r):
            self.events.append(f"{r.status} {r.request.method} {r.url}")
            if r.status >= 400:
                self.found.append(("http", f"HTTP {r.status}: {r.request.method} {r.url}"))

        page.on("console", console)
        page.on("pageerror", lambda e: (self.events.append(f"page error: {e}"), self.found.append(("page", f"uncaught: {e}"))))
        page.on("requestfailed", failed)
        page.on("response", response)

    def outside(self, route):
        seen = []
        outside(route, seen)
        self.found += [("outside", f"request to another host: {u}") for u in seen]

    def expect(self, pattern: str):
        """Console errors, failed requests and HTTP errors matching this are part of the test. Uncaught page errors
        and requests to another host never are."""
        self.expected.append(re.compile(pattern))

    def problems(self) -> list[str]:
        return [text for kind, text in self.found
                if kind in ("page", "outside") or not any(p.search(text) for p in self.expected)]


def requests_for(path: str):
    """"/api/cards" is that one request (whatever its query); a path ending in "/" is everything under it."""
    return re.compile(re.escape(path) + ("" if path.endswith("/") else r"(\?|$)"))


class Hold:
    """Keeps every request for a path waiting (a slow server) until release()."""

    def __init__(self, page, path: str):
        self.waiting, self.open = [], False
        page.route(requests_for(path), self._on)
        page.watch.holds.append(self)

    def _on(self, route):
        if self.open:
            route.continue_()
        else:
            self.waiting.append(route)

    def release(self):
        self.open = True
        while self.waiting:
            try:
                self.waiting.pop().continue_()
            except Error:
                pass  # the page gave up on that request meanwhile


class Break:
    """Makes every request for a path fail until fix(): how="abort" (no answer at all) or an HTTP status."""

    def __init__(self, page, path: str, how="abort"):
        self.how, self.broken, self.hits = how, True, 0
        page.route(requests_for(path), self._on)

    def _on(self, route):
        if not self.broken:
            return route.continue_()
        self.hits += 1
        if self.how == "abort":
            route.abort()
        else:
            route.fulfill(status=self.how, json={"error": "made to fail by the test"})

    def fix(self):
        self.broken = False


def no_saved_numbers(page):
    """As on a dashboard that has never answered: nothing to show while a request is slow or after it fails."""
    page.route("**/api/last", lambda route: route.fulfill(json={}))


def load(page):
    page.goto(page.inst.url + "/")
    page.wait_for_selector(READY)
    return page


@pytest.fixture
def open_page(browser, request):
    """open_page(inst, phone=False, signed_in=True, go=True, clock=False, **context options) -> a watched page.
    go=False leaves it blank so the test can set up routes first; clock=True puts the page's timers under page.clock."""
    made = []

    def dump() -> str:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        stem, lines = re.sub(r"[^\w.-]+", "_", request.node.name).strip("_"), []
        for n, (ctx, page) in enumerate(made):
            shot, log = ARTIFACTS / f"{stem}-{n}.png", ARTIFACTS / f"{stem}-{n}.txt"
            try:
                page.screenshot(path=str(shot), full_page=True)
            except Exception as e:  # a page that can't be pictured still gets its log
                shot = f"(no screenshot: {str(e).splitlines()[0]})"
            log.write_text("\n".join([f"url: {page.url}", "", "--- problems the test did not expect", *page.watch.problems(),
                                      "", "--- console and network", *page.watch.events,
                                      "", "--- dashboard log (last lines)", *page.inst.log.read_text().splitlines()[-40:]]) + "\n")
            lines += [f"screenshot: {shot}", f"log:        {log}"]
        return "\n".join(lines)

    def make(inst, *, phone=False, signed_in=True, go=True, clock=False, **options):
        ctx = browser.new_context(viewport=PHONE if phone else DESK, is_mobile=phone, has_touch=phone,
                                  storage_state=inst.state if signed_in else None, **options)
        page = ctx.new_page()
        page.watch, page.inst = Watch(), inst
        ctx.route(NOT_LOCAL, page.watch.outside)
        page.watch.listen(page)
        if clock:
            page.clock.install()
        made.append((ctx, page))
        return load(page) if go else page

    request.node.ui_dump = dump
    yield make
    problems = [p for _, page in made for p in page.watch.problems()]
    saved = dump() if problems else ""
    for ctx, page in made:
        for hold in page.watch.holds:
            hold.release()
        ctx.close()
    if problems:
        pytest.fail("the page had problems the test did not expect:\n  " + "\n  ".join(problems) + "\n" + saved, pytrace=False)


@pytest.fixture
def desk(full, open_page):
    return open_page(full)


@pytest.fixture
def phone(full, open_page):
    return open_page(full, phone=True)


# ---------- what the tests look at ----------
def cards(page) -> list[dict]:
    """Every card that shows, in page order: id, nav name, whether it is folded, and what it holds."""
    return page.evaluate("""() => [...document.querySelectorAll('.grid > section.card')].filter(s => !s.hidden)
        .map((s, i) => [+getComputedStyle(s).order || 0, i, s]).sort((a, b) => a[0] - b[0] || a[1] - b[1])
        .map(([, , s]) => ({id: s.id, nav: s.dataset.nav, collapsed: s.classList.contains('collapsed'),
            cells: s.querySelectorAll('td').length, charts: [...s.querySelectorAll('canvas')].filter(c => Chart.getChart(c)).length,
            figure: s.querySelector('.kf').textContent.trim()}))""")


def order(page) -> list[str]:
    return [c["id"] for c in cards(page)]


def hidden_cards(page) -> set[str]:
    return set(page.evaluate("() => [...document.querySelectorAll('.grid > section.card')].filter(s => s.hidden).map(s => s.id)"))


def all_loaded(page):
    expect(page.locator(WAITING)).to_have_count(0)
    expect(page.locator("#loadbar")).to_be_hidden()
    expect(page.locator("#loadlist")).to_be_hidden()
    expect(page.locator("#loaderrors")).to_be_empty()


def has_totals(page, net=NET_WORTH, assets=ASSETS, liabilities=LIABILITIES):
    expect(page.locator("#nw")).to_have_text(net)
    expect(page.locator("#assets")).to_have_text(assets)
    expect(page.locator("#liabs")).to_have_text(liabilities)


def fits_the_screen(page):
    """No sideways scrolling, and no card or total sticking out of the window."""
    w = page.evaluate("""() => ({page: document.documentElement.scrollWidth, body: document.body.scrollWidth, window: innerWidth,
        out: [...document.querySelectorAll('.hero > *, .grid > section.card, #status, #jump, #reorderbar, .pagefoot')]
            .filter(e => e.getClientRects().length).map(e => [e.id || e.className, e.getBoundingClientRect()])
            .filter(([, r]) => r.left < -0.5 || r.right > innerWidth + 0.5).map(([name, r]) => `${name}: ${r.left}..${r.right}`)})""")
    assert w["page"] <= w["window"] and w["body"] <= w["window"] and not w["out"], w


def reachable(page, *selectors):
    """Each control shows, lies inside the window's width, and is what a click or tap at its middle lands on."""
    bad = page.evaluate("""sels => sels.flatMap(sel => {
        const els = [...document.querySelectorAll(sel)];
        if (!els.length) return [sel + ": not on the page"];
        return els.map(el => {
            el.scrollIntoView({block: "center", behavior: "instant"});
            const r = el.getBoundingClientRect(), name = `${sel} (${(el.getAttribute("aria-label") || el.textContent).trim().slice(0, 30)})`;
            if (!el.checkVisibility({visibilityProperty: true}) || r.width < 1 || r.height < 1) return name + ": not showing";
            if (r.left < -0.5 || r.right > innerWidth + 0.5) return `${name}: off the side (${r.left}..${r.right} of ${innerWidth})`;
            const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return hit && el.contains(hit) ? null : `${name}: covered by ${hit && (hit.id || hit.className || hit.tagName)}`;
        }).filter(Boolean);
    })""", list(selectors))
    assert not bad, bad
    page.locator(selectors[0]).first.click(trial=True)  # Playwright's own check of one of them


def sync_ended(page):
    """Wait for the dashboard to see the sync over, then let the page's next look (it polls every 2 s) happen now."""
    give_up = time.time() + 15
    while page.request.get(page.inst.url + "/api/sync").json()["running"]:
        assert time.time() < give_up, "the sync never ended"
        time.sleep(0.02)
    page.clock.fast_forward(2000)


def points(page) -> int:
    return page.evaluate("() => Chart.getChart('trend').data.datasets[0].data.length")


def pressed(page, group: str) -> list[str]:
    return page.evaluate("id => [...document.querySelectorAll(`#${id} button[aria-pressed='true']`)].map(b => b.textContent.trim())", group)


# ---------- signing in ----------
def test_signed_out_sees_only_the_login_page(full, open_page):
    page = open_page(full, signed_in=False, go=False)
    page.watch.expect(r"HTTP 401")
    page.goto(full.url + "/")
    expect(page).to_have_url(full.url + "/login")
    expect(page.locator("#u")).to_be_visible()
    for path in ("/healthz", *("/api/" + name for name in API)):
        r = page.request.get(full.url + path)
        assert (r.status, r.json()) == (401, {"error": "unauthorized"}), path
    for path in ("/api/refresh", "/api/sync"):
        assert page.request.post(full.url + path).status == 401, path
    assert full.sync_runs() == 0


def test_login_wrong_password_then_right_then_logout(full, open_page, browser):
    page = open_page(full, signed_in=False, go=False)
    page.watch.expect(r"/login\b")  # the 401 the wrong password gets
    sign_in(page, full, password="not-the-password")  # one wrong try only: five lock the address out for a while
    expect(page.get_by_role("alert")).to_have_text("Wrong username or password.")
    expect(page).to_have_url(full.url + "/login")
    assert not page.context.cookies()

    sign_in(page, full)
    page.wait_for_selector(READY)
    expect(page).to_have_url(full.url + "/")
    has_totals(page)
    session = [c for c in page.context.cookies() if c["name"] == "ctd_session"]
    assert len(session) == 1 and session[0]["httpOnly"] and session[0]["sameSite"] in ("Strict", "Lax"), session
    assert page.request.get(full.url + "/healthz").text().strip() == "ok"

    page.locator("form.logout.hide-sm button").click()
    expect(page).to_have_url(full.url + "/login")
    page.goto(full.url + "/")
    expect(page).to_have_url(full.url + "/login")
    stolen = browser.new_context()  # the cookie from before the logout is worth nothing now
    stolen.add_cookies(session)
    assert stolen.request.get(full.url + "/api/summary").status == 401
    stolen.close()


# ---------- every card, both widths ----------
def test_desktop_every_card_has_its_data(desk):
    has_totals(desk)
    all_loaded(desk)
    expect(desk.locator("#chg")).to_contain_text("30d")
    expect(desk.locator("#nwnote")).to_contain_text("$440,000")  # the sheet's own total, from the canned rows
    shown = cards(desk)
    assert {c["id"] for c in shown} == set(CARD_SAYS) and not hidden_cards(desk)
    for c in shown:
        assert not c["collapsed"] and c["cells"] + c["charts"] > 0, c
        assert c["figure"] or c["id"] == "txcard", c
        expect(desk.locator("#" + c["id"])).to_contain_text(CARD_SAYS[c["id"]])
        expect(desk.locator(f"#{c['id']} .loadbox")).to_have_count(0)
    assert desk.evaluate("ids => ids.filter(id => !Chart.getChart(id))", list(CHARTS)) == []
    expect(desk.locator("#syncpill")).to_contain_text("Synced")
    expect(desk.locator("#problems")).to_be_empty()
    assert desk.title() and desk.locator("#jump button[data-sec]").count() == len(shown)


def test_assets_and_liabilities_tiles_say_whats_behind_the_number(desk):
    """Assets: the categories as one bar in the donut's colours, liquid / retirement / illiquid, the 30-day change.
    Liabilities: the mortgage payoff from the ledger's payments, the cards against their limits, debt as a share of
    assets, the 30-day change; the Liabilities card has its own donut, one slice per line owed, each row's dot in its
    slice's colour, the small card slice still drawn big enough to see."""
    has_totals(desk)
    cats = desk.locator("#cats tr.cat").count()
    expect(desk.locator("#assetbar span")).to_have_count(cats)
    expect(desk.locator("#assetbar span").first).to_have_attribute("title", re.compile(r"^Cash & bank.*: \$[\d,]+ \(\d+\.\d%\)$"))
    expect(desk.locator("#assetsplit")).to_have_text(re.compile(r"^Liquid \$[\d.]+k · Retirement \$[\d.]+k · Illiquid \$[\d.]+k$"))
    expect(desk.locator("#assetchg")).to_have_text(re.compile(r"^30d [+−]\$[\d,]+$"))
    expect(desk.locator("#mortline")).to_have_text(re.compile(r"^Mortgage paid off [A-Z][a-z]{2} 20\d\d at \$1,480/mo · \d+(\.\d+)?%$"))
    expect(desk.locator("#cardline")).to_have_text(re.compile(r"^Cards \$[\d.,]+k? · \d+(\.\d)?% of limits$"))
    expect(desk.locator("#cardline .meter i")).to_have_count(1)
    expect(desk.locator("#liabchg")).to_have_text(re.compile(r"^30d [+−]\$[\d,]+\d+\.\d% of assets$"))
    expect(desk.locator("#liabdonutc")).to_have_text(re.compile(r"^\$[\d.]+k$"))
    slices = desk.evaluate("""() => { const c = Chart.getChart('liabdonut'), d = c.data.datasets[0];
        return d.data.map((v, i) => [c.data.labels[i], v, d.backgroundColor[i]]); }""")
    total = sum(v for _, v, _ in slices)
    assert len(slices) == 2 and min(v for _, v, _ in slices) / total > 0.02, slices  # mortgage + the card, both visible
    desk.locator('#liabcats tr.cat[data-k="cards"]').click()
    dot = desk.locator('#liabcats tr.sub[data-p="cards"] .sw').first
    expect(dot).to_be_visible()
    color = desk.evaluate("el => getComputedStyle(el).backgroundColor", dot.element_handle())
    rgb = desk.evaluate("c => { const e = document.createElement('i'); e.style.color = c; document.body.append(e); const v = getComputedStyle(e).color; e.remove(); return v; }", slices[1][2])
    assert color == rgb, (color, rgb)


def test_desktop_fits_and_controls_are_reachable(desk):
    fits_the_screen(desk)
    reachable(desk, "#syncnow", "#refresh", "#privbtn", "#themepick summary", "form.logout.hide-sm button", "#reorderbtn",
              "#jump button[data-sec]", "#trendrng button", "#trendbench button", ".hero .golink")
    expect(desk.locator(".pagefoot")).to_be_hidden()  # the phone's copy of log out and reorder
    desk.locator('#jump button[data-sec="txcard"]').click()
    expect(desk.locator("#txcard")).to_be_in_viewport()
    fits_the_screen(desk)


def test_phone_fits_and_controls_are_reachable(phone):
    has_totals(phone)
    all_loaded(phone)
    fits_the_screen(phone)
    assert {c["id"] for c in cards(phone) if not c["collapsed"]} == {"draftscard", "trendcard"}  # the rest start folded
    expect(phone.locator("#jump")).to_be_hidden()
    expect(phone.locator("form.logout.hide-sm")).to_be_hidden()
    reachable(phone, "#syncnow", "#refresh", "#privbtn", "#themepick summary", ".hero .golink", "#trendrng button",
              CARDS + " > .sechd", "form.logout.show-sm button", "#reorderbtn2")
    folded = phone.locator(CARDS + ".collapsed > .sechd")
    for _ in range(folded.count()):
        folded.first.click()
    expect(folded).to_have_count(0)
    for c in cards(phone):
        assert c["cells"] + c["charts"] > 0, c
        expect(phone.locator(f"#{c['id']} > .secbody")).to_be_visible()
    fits_the_screen(phone)
    assert phone.evaluate("ids => ids.filter(id => !Chart.getChart(id))", list(CHARTS)) == []


def test_card_folds_and_stays_folded(desk):
    head, card = desk.locator("#holdscard > .sechd"), desk.locator("#holdscard")
    expect(head).to_have_attribute("aria-expanded", "true")
    head.click()
    expect(card).to_have_class(re.compile(r"\bcollapsed\b"))
    expect(head).to_have_attribute("aria-expanded", "false")
    expect(desk.locator("#holdscard > .secbody")).to_be_hidden()
    expect(desk.locator("#kf-holdscard")).to_contain_text("5 positions")  # the folded card still says its figure
    load(desk)
    expect(card).to_have_class(re.compile(r"\bcollapsed\b"))
    head.press("Enter")
    expect(card).not_to_have_class(re.compile(r"\bcollapsed\b"))
    expect(desk.locator("#holds")).to_be_visible()


def test_hero_tiles_jump_to_their_cards(phone):
    for tile, card, rows in (("liability", "liabcard", "liabcats"), ("asset", "alloccard", "cats")):
        expect(phone.locator(f"#{card}")).to_have_class(re.compile(r"\bcollapsed\b"))
        phone.evaluate("scrollTo(0, 0)")
        phone.locator(f'.hero .golink[data-go="{tile}"]').click()
        expect(phone.locator(f"#{card}")).not_to_have_class(re.compile(r"\bcollapsed\b"))
        expect(phone.locator(f"#{card} > .sechd")).to_be_in_viewport()
        expect(phone.locator(f"#{rows} tr.cat").first).to_be_visible()
        expect(phone.locator(f'#{rows} tr.cat[aria-expanded="false"]')).to_have_count(0)  # every line item is out
        expect(phone.locator(f"#{rows} tr.sub").last).to_be_visible()
    expect(phone.locator("#cats tr.sub", has_text="Cold Wallet")).to_be_visible()


def test_hero_tiles_open_the_rows_on_desktop(desk):
    assert desk.locator('#cats tr.cat[aria-expanded="false"]').count() > 0
    expect(desk.locator("#cats tr.sub", has_text="Cold Wallet")).to_be_hidden()
    desk.locator('.hero .golink[data-go="asset"]').press("Enter")
    expect(desk.locator('#cats tr.cat[aria-expanded="false"]')).to_have_count(0)
    expect(desk.locator("#alloccard > .sechd")).to_be_in_viewport()
    desk.locator('.hero .golink[data-go="liability"]').click()
    expect(desk.locator('#liabcats tr.cat[aria-expanded="false"]')).to_have_count(0)
    expect(desk.locator("#liabcats tr.sub", has_text="Credit card")).to_be_visible()
    expect(desk.locator("#cats tr.sub", has_text="Cold Wallet")).to_be_visible()


def test_account_row_shows_its_transactions(desk):
    desk.locator("#accts tr.acct", has_text="Example Bank Checking").click()
    expect(desk.locator("#acctinfo")).to_contain_text(re.compile(r"balance \$6,400\.00 · \d+ transactions"))
    expect(desk.locator("#txh")).to_have_text(re.compile("Transactions: Example Bank Checking"))
    expect(desk.locator("#txcard > .sechd")).to_be_in_viewport()
    expect(desk.locator("#recent")).to_contain_text(re.compile("payroll", re.I))
    expect(desk.locator("#recent")).not_to_contain_text("Example Coffee Roasters")  # a card's charge, not this account's
    desk.locator("#acctclear").click()
    expect(desk.locator("#acctbar")).to_be_hidden()
    expect(desk.locator("#txh")).to_have_text("Recent transactions")
    expect(desk.locator("#recent")).to_contain_text("Example Coffee Roasters")


# ---------- loading ----------
def test_slow_card_shows_loading_until_its_data_lands(full, open_page):
    page = open_page(full, go=False)
    no_saved_numbers(page)
    slow = Hold(page, "/api/cards")
    page.goto(full.url + "/")
    card = page.locator("#cardscard")
    expect(card).to_have_attribute("data-load", "loading")
    assert card.get_attribute("aria-busy") is not None
    expect(card.locator(".loadbox .lbl")).to_contain_text("Loading credit cards…")
    expect(card.locator(".loadbox .lbl")).to_contain_text("gives up at 20s")
    expect(card.locator(".loadbox .skel").first).to_be_visible()
    expect(card.locator(".loadbox .spin")).to_be_visible()
    expect(page.locator("#loadbar")).to_be_visible()
    expect(page.locator("#loadlist")).to_contain_text(re.compile(r"Credit cards….*gives up at 20s"))  # the in-flight line
    expect(page.locator("#loadlist")).to_be_visible()
    moving = page.evaluate("""() => [getComputedStyle(document.getElementById('loadbar'), '::before').animationName,
        getComputedStyle(document.querySelector('#cardscard .spin')).animationName,
        getComputedStyle(document.querySelector('#cardscard .skel')).animationName]""")
    assert "none" not in moving, moving
    # the other cards don't wait for the slow one
    expect(page.locator(WAITING)).to_have_count(1)
    has_totals(page)
    expect(page.locator("#accts")).to_contain_text("Example Bank Checking")
    assert page.evaluate("document.body.dataset.ready") is None

    slow.release()
    expect(card).to_contain_text("Example Rewards Card")
    expect(card).not_to_have_attribute("aria-busy", re.compile(""))
    expect(card.locator(".loadbox")).to_have_count(0)
    all_loaded(page)
    page.wait_for_selector(READY)


def test_slow_totals_show_placeholders_then_numbers(full, open_page):
    page = open_page(full, go=False)
    no_saved_numbers(page)
    slow = Hold(page, "/api/summary")
    page.goto(full.url + "/")
    expect(page.locator("#hero")).to_have_attribute("data-load", "loading")
    for box in ("#nw", "#assets", "#liabs"):
        expect(page.locator(f"{box} .skel")).to_be_visible()
    expect(page.locator("#status")).to_contain_text("Loading…")
    expect(page.locator("#alloccard")).to_have_attribute("data-load", "loading")
    expect(page.locator("#alloccard .loadbox .lbl")).to_contain_text("Loading net worth and balance sheet…")
    expect(page.locator("#loadlist")).to_contain_text(re.compile("net worth and balance sheet", re.I))
    expect(page.locator("#holds")).to_contain_text("Total Market Fund")  # a card that doesn't need the summary
    slow.release()
    has_totals(page)
    expect(page.locator("#syncpill")).to_contain_text("Synced")
    all_loaded(page)


def test_slow_answers_show_the_last_saved_numbers_meanwhile(full, open_page):
    page = open_page(full, go=False)
    slow = [Hold(page, "/api/summary"), Hold(page, "/api/cards")]
    page.goto(full.url + "/")
    expect(page.locator("#cardscard")).to_have_attribute("data-load", "old")
    expect(page.locator("#hero")).to_have_attribute("data-load", "old")
    has_totals(page)  # the numbers the dashboard last answered with, marked as such
    expect(page.locator("#cardscard")).to_contain_text("Example Rewards Card")
    expect(page.locator("#cardscard .loadbox")).to_have_count(0)
    expect(page.locator("#loadbar")).to_be_visible()
    expect(page.locator("#loadlist")).to_contain_text(re.compile(r"Showing the numbers from .* meanwhile"))
    page.wait_for_timeout(800)  # past when the old fade would have started and finished
    faded = page.evaluate("""() => ['#cardscard > .secbody', '#hero .big', '#hero .mid', '#hero .chips']
        .flatMap(q => [...document.querySelectorAll(q)]).map(e => [getComputedStyle(e).opacity, getComputedStyle(e).filter])
        .filter(([o, f]) => o !== '1' || f !== 'none')""")
    assert faded == [], faded  # the earlier numbers stay at full strength while new ones load
    for hold in slow:
        hold.release()
    all_loaded(page)
    has_totals(page)


def test_reduced_motion_stills_the_loading_signs(full, open_page):
    page = open_page(full, go=False, reduced_motion="reduce")
    no_saved_numbers(page)
    slow = Hold(page, "/api/cards")
    page.goto(full.url + "/")
    expect(page.locator("#cardscard .loadbox .spin")).to_be_visible()
    still = page.evaluate("""() => [getComputedStyle(document.getElementById('loadbar'), '::before').animationName,
        getComputedStyle(document.querySelector('#cardscard .spin')).animationName,
        getComputedStyle(document.querySelector('#cardscard .skel')).animationName,
        getComputedStyle(document.querySelector('.hero .golink')).transitionDuration]""")
    assert still == ["none", "none", "none", "0s"], still
    expect(page.locator("#loadbar")).to_be_visible()  # it still says "loading", without the movement
    slow.release()
    all_loaded(page)
    has_totals(page)
    page.locator('.hero .golink[data-go="liability"]').click()
    expect(page.locator('#liabcats tr.cat[aria-expanded="false"]')).to_have_count(0)


# ---------- failures ----------
@pytest.mark.parametrize("how, reason", [(500, "the server answered with an error (HTTP 500)"), ("abort", "can't reach the server")])
def test_failed_card_says_so_and_retry_recovers(full, open_page, how, reason):
    page = open_page(full, go=False)
    page.watch.expect(r"holdings")
    no_saved_numbers(page)
    broken = Break(page, "/api/holdings", how)
    load(page)
    card = page.locator("#holdscard")
    expect(card).to_have_attribute("data-load", "error")
    box = card.locator(".loadbox.failed")
    expect(box).to_have_attribute("role", "alert")
    expect(box).to_contain_text("Couldn't load this section.")
    expect(box).to_contain_text(reason)
    expect(page.locator("#loaderrors")).to_contain_text("Couldn't load: Holdings.")
    expect(page.locator(WAITING)).to_have_count(1)  # only that card
    has_totals(page)
    expect(page.locator("#loadbar")).to_be_hidden()

    box.get_by_role("button", name="Retry").click()  # still down: it fails again and says so again
    expect(card.locator(".loadbox.failed")).to_contain_text(reason)
    assert broken.hits >= 2
    broken.fix()
    card.get_by_role("button", name="Retry").click()
    expect(card).to_contain_text("Total Market Fund")
    all_loaded(page)


def test_no_answer_gives_up_after_20_seconds_and_retry_recovers(full, open_page):
    page = open_page(full, go=False, clock=True)
    page.watch.expect(r"cards")
    no_saved_numbers(page)
    slow = Hold(page, "/api/cards")
    page.goto(full.url + "/")
    card = page.locator("#cardscard")
    expect(card).to_have_attribute("data-load", "loading")
    page.clock.fast_forward(12_000)
    expect(card).to_have_attribute("data-load", "loading")
    expect(card.locator(".loadbox .lbl")).to_contain_text(re.compile(r"Loading credit cards… 1\ds"))
    page.clock.fast_forward(9_000)  # 21 s in all, however long the page took to get here
    expect(card).to_have_attribute("data-load", "error")
    expect(card.locator(".loadbox.failed")).to_contain_text("no answer after 20 seconds")
    expect(page.locator("#loaderrors")).to_contain_text("Couldn't load: Cards.")
    expect(page.locator("#loadbar")).to_be_hidden()
    slow.release()
    page.locator("#loaderrors").get_by_role("button", name="Retry").click()  # the retry at the top does the same
    expect(card).to_contain_text("Example Rewards Card")
    all_loaded(page)


def test_server_down_then_back(full, open_page):
    page = open_page(full, go=False)
    page.watch.expect(r"/api/|^console\.error")
    down = Break(page, "/api/")
    load(page)
    expect(page.locator("#hero")).to_have_attribute("data-load", "error")
    for box in ("#nw", "#assets", "#liabs"):
        expect(page.locator(f"{box} .loadfail")).to_contain_text("Couldn't load")
    expect(page.locator("#status")).to_contain_text("Couldn't load")
    expect(page.locator(CARDS + '[data-load="error"]')).to_have_count(len(CARD_SAYS))
    expect(page.locator("#loaderrors")).to_contain_text("Couldn't load: Net worth")
    expect(page.locator("#loadbar")).to_be_hidden()
    down.fix()
    page.locator("#status").get_by_role("button", name="Retry").click()
    has_totals(page)
    all_loaded(page)
    assert {c["id"] for c in cards(page)} == set(CARD_SAYS)


def test_failed_reread_keeps_the_numbers_and_retry_recovers(desk):
    desk.watch.expect(r"refresh|summary Error")  # the page logs the failed re-read under "summary"
    broken = Break(desk, "/api/refresh", 500)
    button = desk.locator("#refresh")
    expect(button).to_have_text("Re-read sheet")
    button.click()
    expect(desk.locator("#loaderrors")).to_contain_text("Couldn't refresh: Balance sheet re-read.")
    assert broken.hits == 1
    has_totals(desk)  # the plain numbers still loaded
    expect(desk.locator(WAITING)).to_have_count(0)
    expect(button).to_be_enabled()
    expect(button).to_have_text("Re-read sheet")
    broken.fix()
    with desk.expect_response(lambda r: r.url.endswith("/api/refresh") and r.request.method == "POST") as reread:
        desk.locator("#loaderrors").get_by_role("button", name="Retry").click()
    assert reread.value.status == 200
    all_loaded(desk)
    has_totals(desk)


def test_refresh_failure_keeps_the_old_numbers(desk):
    desk.watch.expect(r"summary|refresh")
    broken = [Break(desk, "/api/refresh", 500), Break(desk, "/api/summary", "abort"), Break(desk, "/api/accounts", 500)]
    desk.watch.expect(r"accounts")
    desk.locator("#refresh").click()
    expect(desk.locator("#loaderrors")).to_contain_text(re.compile(r"Couldn't refresh: .*Net worth.*Showing the numbers from"))
    has_totals(desk)  # what was on screen stays
    expect(desk.locator("#accts")).to_contain_text("Example Bank Checking")
    expect(desk.locator("#acctscard")).to_have_attribute("data-load", "stale")
    expect(desk.locator("#acctscard .loadbox.stale")).to_contain_text("Couldn't refresh this section. Showing the numbers from")
    expect(desk.locator("#unrefreshed")).to_contain_text("Couldn't refresh · showing numbers from")
    for b in broken:
        b.fix()
    desk.locator("#acctscard .loadbox.stale").get_by_role("button", name="Retry").click()  # one card's own retry
    expect(desk.locator("#acctscard")).not_to_have_attribute("data-load", "stale")
    desk.locator("#loaderrors").get_by_role("button", name="Retry").click()
    all_loaded(desk)
    expect(desk.locator("#unrefreshed")).to_have_count(0)
    has_totals(desk)


def test_page_opened_while_the_summary_fails_shows_the_last_saved_numbers(full, open_page):
    page = open_page(full, go=False)
    page.watch.expect(r"summary")
    broken = Break(page, "/api/summary", 500)
    load(page)
    has_totals(page)  # from the dashboard's saved copy (/api/last)
    expect(page.locator("#hero")).to_have_attribute("data-load", "stale")
    expect(page.locator("#alloccard .loadbox.stale")).to_contain_text("Couldn't refresh this section. Showing the numbers from")
    expect(page.locator("#cats")).to_contain_text("Cash & bank")
    expect(page.locator("#unrefreshed")).to_contain_text("Couldn't refresh · showing numbers from")
    expect(page.locator("#loaderrors")).to_contain_text(re.compile(r"Couldn't refresh: Net worth\. Showing the numbers from"))
    broken.fix()
    page.locator("#loaderrors").get_by_role("button", name="Retry").click()
    all_loaded(page)
    expect(page.locator("#unrefreshed")).to_have_count(0)
    has_totals(page)


# ---------- the sync pill ----------
def test_sync_pill_after_a_clean_sync(desk):
    pill = desk.locator("#syncpill")
    expect(pill).to_contain_text("Synced")
    expect(pill).not_to_have_class(re.compile(r"\bsyncbad\b"))
    expect(pill.locator(".dot")).not_to_have_class(re.compile(r"\bbad\b"))
    expect(desk.locator("#problems")).to_be_empty()
    expect(desk.locator("#syncnow")).to_have_text("Sync now")
    expect(desk.locator("#syncnow")).to_be_enabled()


def test_sync_pill_after_a_sync_with_problems(full, open_page):
    full.set_status(rc=1, problems=("Example Bank: the connection needs to be renewed", "Example Card Co: no answer from the bank"))
    page = open_page(full)
    pill = page.locator("#syncpill")
    expect(pill).to_contain_text("Sync problem")
    expect(pill).to_contain_text("last ran")
    expect(pill).to_have_class(re.compile(r"\bsyncbad\b"))
    expect(pill.locator(".dot")).to_have_class(re.compile(r"\bbad\b"))
    box = page.locator("#problems details")
    expect(box.locator("summary")).to_contain_text("2 data warnings")
    expect(box).not_to_have_attribute("open", "")
    pill.click()  # the pill opens the list
    expect(box).to_have_attribute("open", "")
    expect(box).to_contain_text("Example Bank: the connection needs to be renewed")
    expect(box).to_contain_text("Example Card Co: no answer from the bank")
    has_totals(page)  # a sync problem doesn't hide the numbers

    full.set_status(rc=3)  # an exit code and nothing else: the page says that much itself
    load(page)
    expect(page.locator("#syncpill")).to_contain_text("Sync problem")
    expect(page.locator("#problems")).to_contain_text("The last sync ended with an error (exit code 3)")


def test_sync_pill_on_a_phone_keeps_to_its_row(full, open_page):
    full.set_status(rc=1, problems=("Example Bank: the connection needs to be renewed",))
    page = open_page(full, phone=True)
    expect(page.locator("#syncpill")).to_contain_text("Sync problem")
    expect(page.locator("#problems")).to_contain_text("Example Bank: the connection needs to be renewed")
    fits_the_screen(page)
    reachable(page, "#syncnow", "#refresh")


# ---------- Sync now (the instance's sync command is a stand-in script; no bank is called) ----------
def test_sync_now_runs_the_sync_and_updates_the_numbers(full, open_page):
    page = open_page(full, clock=True)
    button, note = page.locator("#syncnow"), page.locator("#syncnote")
    full.hold_sync()  # the stand-in sync keeps running until released
    button.click()
    expect(button).to_contain_text("Syncing…")
    expect(button).to_be_disabled()
    expect(page.locator("#loadbar")).to_be_visible()
    expect(page.locator("#loadlist")).to_contain_text("Syncing banks…")
    assert page.request.get(full.url + "/api/sync").json()["running"] is True
    assert page.request.post(full.url + "/api/sync").json() == {"started": False, "running": True, "message": "A sync is already running"}
    page.clock.fast_forward(2000)  # a look while it runs changes nothing
    expect(button).to_be_disabled()
    has_totals(page)
    with page.expect_response(lambda r: r.url.endswith("/api/refresh")) as reread:  # the numbers are read again after it
        full.release_sync()
        sync_ended(page)
    assert reread.value.status == 200
    expect(note).to_contain_text(re.compile(r"Sync finished in \d+s · numbers updated"))
    expect(note).not_to_have_class(re.compile(r"\bbad\b"))
    expect(button).to_have_text("Sync now")
    expect(button).to_be_enabled()
    expect(page.locator("#syncpill")).to_contain_text("Synced")
    all_loaded(page)
    has_totals(page)
    assert full.sync_runs() == 1
    note.get_by_role("button", name="Dismiss").click()
    expect(note).to_be_empty()

    page.watch.expect(r"429")
    button.click()  # straight after a sync: the 15-minute gap
    expect(note).to_contain_text(re.compile(r"(Synced|Last sync ran) less than a minute ago — try again in 15 min"))
    assert full.sync_runs() == 1


def test_sync_now_reports_a_sync_that_failed(full, open_page):
    page = open_page(full, clock=True)
    full.plan_sync(rc=1, problems=("Example Bank: the connection needs to be renewed",))
    page.locator("#syncnow").click()
    sync_ended(page)
    note = page.locator("#syncnote")
    expect(note).to_contain_text("The sync ended with an error:")
    expect(note.locator("li")).to_have_text(["Example Bank: the connection needs to be renewed"])
    expect(note).to_have_class(re.compile(r"\bbad\b"))
    expect(page.locator("#syncpill")).to_contain_text("Sync problem")
    expect(page.locator("#problems")).to_contain_text("Example Bank: the connection needs to be renewed")
    expect(page.locator("#syncnow")).to_be_enabled()
    has_totals(page)
    assert full.sync_runs() == 1
    page.clock.fast_forward(60_000)  # an error stays until dismissed
    expect(note).to_contain_text("The sync ended with an error:")


@pytest.mark.parametrize("status_age_min, starts_min_ago, says", [
    (5, [], r"Synced 5 min ago — try again in 10 min"),
    (120, [5], r"Last sync ran 5 min ago — try again in 10 min"),
    (120, [30 + 60 * k for k in range(8)], r"Sync now has run 8 times in the last 24 hours, its daily limit — try again in 16 h \d\d min"),
])
def test_sync_now_says_when_it_is_too_soon(full, open_page, status_age_min, starts_min_ago, says):
    full.set_status(age_min=status_age_min)
    full.sync_starts(starts_min_ago)
    page = open_page(full)
    page.watch.expect(r"429")
    page.locator("#syncnow").click()
    note = page.locator("#syncnote")
    expect(note).to_contain_text(re.compile(says))
    expect(note).not_to_have_class(re.compile(r"\bbad\b"))  # a wait, not an error
    expect(page.locator("#syncnow")).to_have_text("Sync now")
    expect(page.locator("#syncnow")).to_be_enabled()
    expect(page.locator("#loadbar")).to_be_hidden()
    assert full.sync_runs() == 0
    note.get_by_role("button", name="Dismiss").click()
    expect(note).to_be_empty()


def test_sync_already_running_is_followed_not_started_twice(full, open_page):
    page = open_page(full, clock=True)
    with open(full.lock, "a") as lock:  # the daily sync, as far as the dashboard can tell
        fcntl.flock(lock, fcntl.LOCK_EX)
        page.locator("#syncnow").click()
        note, button = page.locator("#syncnote"), page.locator("#syncnow")
        expect(note).to_contain_text("A sync is already running · this page will update when it finishes")
        expect(button).to_contain_text("Syncing…")
        expect(button).to_be_disabled()

        load(page)  # a page opened while one runs follows it too
        expect(note).to_contain_text("A sync is already running · this page will update when it finishes")
        expect(button).to_be_disabled()
        expect(page.locator("#loadlist")).to_contain_text("Syncing banks…")
        has_totals(page)
        fcntl.flock(lock, fcntl.LOCK_UN)
    sync_ended(page)
    expect(note).to_contain_text("Sync finished · numbers updated")
    expect(button).to_have_text("Sync now")
    expect(button).to_be_enabled()
    all_loaded(page)
    assert full.sync_runs() == 0  # the button started nothing


def test_sync_now_without_a_bank_connection(full, open_page):
    page = open_page(full)
    page.watch.expect(r"409")
    link = full.conf / "simplefin_access_url"
    kept = link.read_text()
    link.unlink()
    try:
        page.locator("#syncnow").click()
        note = page.locator("#syncnote")
        expect(note).to_contain_text("No bank connection yet: link SimpleFIN first")
        expect(note).to_have_class(re.compile(r"\bbad\b"))
        expect(page.locator("#syncnow")).to_be_enabled()
    finally:
        link.write_text(kept)
    assert full.sync_runs() == 0


def test_sync_now_on_a_phone(full, open_page):
    page = open_page(full, phone=True, clock=True)
    button = page.locator("#syncnow")
    full.hold_sync()
    button.click()
    expect(button).to_contain_text("Syncing…")
    expect(button.locator(".hide-sm")).to_be_hidden()  # the seconds are in the wait line, not the narrow header
    expect(page.locator("#loadlist")).to_contain_text("Syncing banks…")
    fits_the_screen(page)
    full.release_sync()
    sync_ended(page)
    expect(page.locator("#syncnote")).to_contain_text("numbers updated")
    fits_the_screen(page)
    assert full.sync_runs() == 1


# ---------- the trend ----------
def test_trend_range_buttons(desk):
    ranges = ["1W", "1M", "3M", "6M", "YTD", "1Y", "3Y", "All"]
    assert desk.locator("#trendrng button").all_text_contents() == ranges
    assert pressed(desk, "trendrng") == ["All"]  # there are sheet points from before the daily snapshots
    expect(desk.locator("#trendsub")).to_contain_text(f"8 sheet + {F.DAYS} daily snapshots")
    count = {}
    for r in ranges:
        desk.locator(f'#trendrng button[data-r="{r}"]').click()
        assert pressed(desk, "trendrng") == [r]
        if r != "YTD":  # on New Year's Day that is a single point
            expect(desk.locator("#trendchg")).not_to_be_empty()
        count[r] = points(desk)
    assert 5 <= count["1W"] <= 8 < count["1M"] <= 32 < count["3M"] <= count["1Y"] <= count["All"] == 8 + F.DAYS, count
    desk.locator('#trendrng button[data-r="1W"]').click()
    expect(desk.locator("#kf-trendcard")).to_contain_text("this week")
    assert desk.evaluate("localStorage.getItem('wl.trendRange')") == "1W"
    load(desk)  # the range is remembered
    assert pressed(desk, "trendrng") == ["1W"] and points(desk) == count["1W"]


def test_correlation_pairs_each_point_with_the_same_days_close(desk):
    """A daily point is its date's value (investments at that day's close, a weekend carrying Friday's): net worth that
    moves exactly with the S&P 500 on the same day correlates at 1, weekend points add no change, and the same moves
    one day late (the old feed's lag) do not."""
    r = desk.evaluate("""() => {
      const dates = [], spx = [], lag = [];
      for (let i = 0; i < 14; i++) {  // Mon 2026-11-02 .. Sun 11-15: weekday closes, weekends carry Friday's
        const d = new Date(2026, 10, 2 + i, 12), wd = d.getDay(), iso = `2026-11-${String(2 + i).padStart(2, "0")}`;
        const l = wd === 6 ? 1 : wd === 0 ? 2 : 0;
        dates.push(iso); lag.push(l);
        spx.push(l ? spx.at(-1) : 6000 * (1 + 0.01 * Math.sin(1.7 * i)));
      }
      const t = {benchmarks: {dates, prices: {SPX: spx}, lag: {SPX: lag}}};
      const pts = (shift) => dates.map((date, i) => ({x: dayMs(date), y: 1e6 * spx[Math.max(0, i - shift)] / spx[0],
        date, kind: "daily", close: dates[i - lag[i]]}));
      return [benchCorr(t, "USD", pts(0), "SPX"), benchCorr(t, "USD", pts(1), "SPX")];
    }""")
    same, late = r
    assert same["r"] == pytest.approx(1.0) and same["n"] == 9  # 10 weekdays -> 9 changes; the weekend adds none
    assert late["r"] < 0.9


def test_correlation_chips_are_plain_numbers_for_every_range(desk):
    chips, plain = desk.locator("#trendbench button .corr"), re.compile(r"^[−-]?[01]\.\d\d$")
    for r in ("1M", "1W"):  # all daily snapshots: measured, however few changes a week has
        desk.locator(f'#trendrng button[data-r="{r}"]').click()
        expect(chips).to_have_text([plain] * 3)
    expect(desk.locator('#trendbench button[data-b="SPX"]')).to_have_attribute("title", re.compile(r"correlation [−-]?[01]\.\d\d .*\(\d+ changes, daily snapshots only\)"))
    desk.locator('#trendrng button[data-r="All"]').click()  # back before the dailies: interpolated between sheet points
    expect(chips).to_have_text([plain] * 3)  # shown like a measured figure: no ≈, no est tag, no note
    expect(desk.locator("#trendbench .corr small, #trendbench .corr.est, #trendcorrnote")).to_have_count(0)
    assert desk.evaluate("""() => [...document.querySelectorAll('#trendbench .corr')].every(e =>
        getComputedStyle(e).fontStyle === 'normal' && getComputedStyle(e).opacity === '1')""")
    expect(desk.locator('#trendbench button[data-b="SPX"]')).to_have_attribute("title", re.compile(
        r"correlation [−-]?[01]\.\d\d .*net worth is interpolated between balance-sheet snapshots"))
    assert "estimat" not in desk.locator('#trendbench button[data-b="SPX"]').get_attribute("title").lower()
    spx = desk.locator('#trendbench button[data-b="SPX"]')
    lines = desk.evaluate("Chart.getChart('trend').data.datasets.length")
    spx.click()  # a chip draws its benchmark over the trend
    expect(spx).to_have_attribute("aria-pressed", "true")
    assert desk.evaluate("Chart.getChart('trend').data.datasets.length") == lines + 1
    spx.click()
    expect(spx).to_have_attribute("aria-pressed", "false")
    assert desk.evaluate("Chart.getChart('trend').data.datasets.length") == lines


# ---------- reordering the cards ----------
def test_reorder_cards_on_desktop_persists(desk):
    start, n = order(desk), len(CARD_SAYS)
    first = next(c for c in cards(desk) if c["id"] == start[0])
    desk.locator("#reorderbtn").click()
    expect(desk.locator("body")).to_have_class(re.compile(r"\breordering\b"))
    expect(desk.locator("#reorderbar")).to_be_visible()
    expect(desk.locator("#reordermsg")).to_have_text("Drag a heading, or use the arrows.")
    expect(desk.locator("#reorderreset")).to_be_disabled()
    expect(desk.locator(f"#{start[0]} > .secbody")).to_be_hidden()  # headings only while reordering
    expect(desk.get_by_role("button", name=f"Move {first['nav']} up", exact=True)).to_be_disabled()
    desk.get_by_role("button", name=f"Move {first['nav']} down", exact=True).click()
    expect(desk.locator("#reordermsg")).to_have_text(f"{first['nav']} is now 2 of {n}.")
    desk.get_by_role("button", name=f"Move {first['nav']} down", exact=True).click()
    expect(desk.locator("#reordermsg")).to_have_text(f"{first['nav']} is now 3 of {n}.")
    moved = [start[1], start[2], start[0], *start[3:]]
    assert order(desk) == moved
    assert desk.evaluate("JSON.parse(localStorage.getItem('wl.order'))") == moved
    expect(desk.locator("#reorderreset")).to_be_enabled()
    fits_the_screen(desk)
    desk.keyboard.press("Escape")
    expect(desk.locator("#reorderbar")).to_be_hidden()
    expect(desk.locator("body")).not_to_have_class(re.compile(r"\breordering\b"))
    expect(desk.locator(f"#{start[0]} > .secbody")).to_be_visible()

    load(desk)
    assert order(desk) == moved
    assert desk.evaluate("[...document.querySelectorAll('#jump button[data-sec]')].map(b => b.dataset.sec)") == moved
    tops = desk.evaluate("ids => ids.map(id => { const r = document.getElementById(id).getBoundingClientRect(); return [Math.round(r.top), Math.round(r.left)]; })", moved)
    assert tops == sorted(tops), tops  # what the page says is the order is the order on screen
    fits_the_screen(desk)

    desk.locator("#reorderbtn").click()
    desk.locator("#reorderreset").click()
    expect(desk.locator("#reordermsg")).to_have_text("Back to the default order.")
    assert order(desk) == start and desk.evaluate("localStorage.getItem('wl.order')") is None
    desk.locator("#reorderdone").click()
    expect(desk.locator("#reorderbar")).to_be_hidden()
    load(desk)
    assert order(desk) == start


def test_reorder_cards_on_a_phone_persists(phone):
    start, by_id = order(phone), {c["id"]: c["nav"] for c in cards(phone)}
    phone.locator("#reorderbtn2").click()
    expect(phone.locator("#reorderbar")).to_be_visible()
    expect(phone.locator("#reordermsg")).to_have_text("Move a card with its arrows.")
    fits_the_screen(phone)
    third = by_id[start[2]]
    reachable(phone, CARDS + " .mv button:not(:disabled)", "#reorderdone")
    phone.get_by_role("button", name=f"Move {third} up", exact=True).click()
    expect(phone.locator("#reordermsg")).to_have_text(f"{third} is now 2 of {len(start)}.")
    moved = [start[0], start[2], start[1], *start[3:]]
    assert order(phone) == moved
    phone.locator("#reorderdone").click()
    expect(phone.locator("#reorderbar")).to_be_hidden()
    load(phone)
    assert order(phone) == moved
    tops = phone.evaluate("ids => ids.map(id => Math.round(document.getElementById(id).getBoundingClientRect().top))", moved)
    assert tops == sorted(tops), tops
    fits_the_screen(phone)


# ---------- themes, privacy ----------
def test_themes_switch_and_are_remembered(desk):
    look = "() => [getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(), getComputedStyle(document.body).backgroundColor]"
    seen = {"graphite": desk.evaluate(look)}
    assert desk.evaluate("document.documentElement.dataset.theme || 'graphite'") == "graphite"
    for theme in ("rose", "amethyst", "plum", "sage"):
        desk.locator("#themepick summary").click()
        desk.locator(f'#themes button[data-t="{theme}"]').click()
        expect(desk.locator("html")).to_have_attribute("data-theme", theme)
        expect(desk.locator(f'#themes button[data-t="{theme}"]')).to_have_attribute("aria-pressed", "true")
        expect(desk.locator("#themepick")).not_to_have_attribute("open", "")  # picking closes the picker
        seen[theme] = desk.evaluate(look)
    assert len({tuple(v) for v in seen.values()}) == 5, seen  # five different looks
    assert desk.evaluate("localStorage.getItem('wl.theme')") == "sage"
    load(desk)
    expect(desk.locator("html")).to_have_attribute("data-theme", "sage")
    assert desk.evaluate(look) == seen["sage"]
    has_totals(desk)
    assert desk.evaluate("ids => ids.filter(id => !Chart.getChart(id))", list(CHARTS)) == []  # the charts redraw in it
    fits_the_screen(desk)


def test_privacy_mode_hides_every_amount(desk):
    button = desk.locator("#privbtn")
    expect(button).to_have_attribute("aria-pressed", "false")
    button.click()
    expect(button).to_have_attribute("aria-pressed", "true")
    expect(desk.locator("#nw")).to_have_text("$•••")
    expect(desk.locator("#assets")).to_have_text("$•••")
    text = desk.locator(".wrap").inner_text()
    assert not re.search(r"\$\s?\d", text), re.findall(r".{20}\$\s?\d.{10}", text)[:5]
    load(desk)  # it is remembered
    expect(desk.locator("#privbtn")).to_have_attribute("aria-pressed", "true")
    expect(desk.locator("#nw")).to_have_text("$•••")
    all_loaded(desk)
    desk.locator("#privbtn").click()
    has_totals(desk)


# ---------- SimpleFIN-only: no personal.json, no spreadsheet ----------
SFO_TOTALS = dict(net=f"${F.SYNCED_ASSETS - F.LIABILITIES:,.0f}", assets=f"${F.SYNCED_ASSETS:,.0f}", liabilities=LIABILITIES)


def test_simplefin_only_hides_the_optional_cards(sfo, open_page):
    page = open_page(sfo)
    has_totals(page, **SFO_TOTALS)
    all_loaded(page)
    assert hidden_cards(page) == SHEET_ONLY
    shown = cards(page)
    assert {c["id"] for c in shown} == set(CARD_SAYS) - SHEET_ONLY
    for c in shown:
        assert c["cells"] + c["charts"] > 0, c
        expect(page.locator("#" + c["id"])).to_be_visible()
    for name in ("cardscard", "acctscard", "holdscard", "txcard"):
        expect(page.locator("#" + name)).to_contain_text(CARD_SAYS[name])
    assert set(page.evaluate("[...document.querySelectorAll('#jump button[data-sec]')].filter(b => !b.hidden).map(b => b.dataset.sec)")) == {c["id"] for c in shown}
    expect(page.locator("body")).to_have_class(re.compile(r"\bsheetless\b"))
    expect(page.locator("#syncpill")).to_contain_text("Synced")
    expect(page.locator("#problems")).to_be_empty()
    assert pressed(page, "trendrng") == ["1M"]  # nothing older than the daily snapshots to show
    expect(page.locator("#trendbench button .corr")).to_have_text([re.compile(r"^[−-]?[01]\.\d\d$")] * 3)
    fits_the_screen(page)

    button = page.locator("#refresh")  # no sheet to re-read: the button reloads the numbers
    expect(button).to_have_text("Reload")
    with page.expect_response(lambda r: "/api/summary" in r.url or "/api/refresh" in r.url) as answer:
        button.click()
    assert answer.value.status == 200
    expect(button).to_have_text("Reload")
    expect(button).to_be_enabled()
    all_loaded(page)
    has_totals(page, **SFO_TOTALS)
    assert hidden_cards(page) == SHEET_ONLY


def test_simplefin_only_on_a_phone(sfo, open_page):
    page = open_page(sfo, phone=True, clock=True)
    has_totals(page, **SFO_TOTALS)
    all_loaded(page)
    assert hidden_cards(page) == SHEET_ONLY
    fits_the_screen(page)
    folded = page.locator(CARDS + ".collapsed:not([hidden]) > .sechd")
    for _ in range(folded.count()):
        folded.first.click()
    expect(folded).to_have_count(0)
    fits_the_screen(page)
    reachable(page, "#syncnow", "#refresh", "form.logout.show-sm button", "#reorderbtn2")
    page.locator("#syncnow").click()  # the sync works the same without a personal.json
    sync_ended(page)
    expect(page.locator("#syncnote")).to_contain_text("numbers updated")
    has_totals(page, **SFO_TOTALS)
    assert sfo.sync_runs() == 1


# ---------- live prices in the markets line ----------
def test_markets_line_is_live_and_rereads_quietly(full, open_page):
    """Bitcoin, gold and the S&P 500 at live prices in the line above the totals; a minute later the page re-reads just
    that line by itself, with no loading bar and nothing dimmed. A source that is down shows its stored close, and the
    totals stay at the daily run's prices throughout."""
    full.set_live_markets({"BTC": 123_456.0, "XAU": 4321.0, "SPX": 6789.0})
    page = open_page(full, clock=True)
    bar = page.locator(".mktbar")
    expect(bar.locator("[data-live]")).to_have_count(3)
    expect(bar).to_contain_text("$123,456")
    expect(bar.locator(".upd")).to_have_text(re.compile(r"^Live \d+:\d\d [AP]M$"))
    has_totals(page)
    all_loaded(page)
    full.set_live_markets({"BTC": 124_000.0, "SPX": 6789.0})  # gold's source is down
    slow = Hold(page, "/api/markets")
    page.clock.fast_forward(61_000)
    for _ in range(200):  # until the page's own re-read is out
        if slow.waiting:
            break
        page.wait_for_timeout(25)
    assert slow.waiting, "the page did not re-read the markets line"
    expect(bar).to_contain_text("$123,456")  # the numbers on screen stay meanwhile
    all_loaded(page)  # no loading bar, list or card state for a quiet re-read
    faded = page.evaluate("""() => ['.mktbar', '.mktbar *', '.grid > section.card > .secbody', '#hero .big']
        .flatMap(q => [...document.querySelectorAll(q)]).map(e => [getComputedStyle(e).opacity, getComputedStyle(e).filter])
        .filter(([o, f]) => o !== '1' || f !== 'none')""")
    assert faded == [], faded
    slow.release()
    expect(bar).to_contain_text("$124,000")
    expect(bar.locator("[data-live]")).to_have_count(2)
    expect(bar.locator("div:not([data-live]) > .n", has_text="Gold")).to_have_count(1)  # its stored close
    has_totals(page)
    all_loaded(page)


def test_sign_in_page_shows_the_same_live_markets_line_and_rereads_it(full, open_page):
    """Signed out, the line under the card has the dashboard's live prices and re-reads them every minute the same way
    (no session needed for /api/markets, nothing else opened up)."""
    full.set_live_markets({"BTC": 123_456.0, "XAU": 4321.0, "SPX": 6789.0})
    dash = open_page(full)
    out = open_page(full, signed_in=False, go=False, clock=True)
    out.goto(full.url + "/login")
    line = lambda page: page.locator(".mkt").inner_text()
    expect(out.locator(".mkt [data-live]")).to_have_count(3)
    expect(out.locator(".mktbar .upd")).to_have_text(re.compile(r"^Live \d+:\d\d [AP]M$"))
    assert line(out) == line(dash) and "$123,456" in line(out)
    full.set_live_markets({"BTC": 124_000.0, "XAU": 4321.0, "SPX": 6789.0})
    out.clock.fast_forward(61_000)
    expect(out.locator(".mkt")).to_contain_text("$124,000")
    assert out.evaluate("() => [...document.querySelectorAll('.mktbar *')].every(e => getComputedStyle(e).opacity === '1')")
    expect(out.locator("#u")).to_be_visible()
    assert out.request.get(full.url + "/api/summary").status == 401  # only the line is public


def test_markets_line_is_not_reread_with_live_prices_off(full, open_page):
    page = open_page(full, clock=True)  # no live prices: the stored closes, and nothing to re-read
    asked = []
    page.on("request", lambda r: "/api/markets" in r.url and asked.append(r.url))
    expect(page.locator(".mktbar .mkt")).to_be_visible()
    expect(page.locator(".mktbar [data-live]")).to_have_count(0)
    page.clock.fast_forward(181_000)
    page.wait_for_timeout(200)
    assert asked == []


# ---------- the page's one outside file ----------
def test_page_loads_nothing_else_from_outside_and_the_test_copy_of_chartjs_is_the_real_one():
    html = (HERE / "index.html").read_text()
    urls = set(re.findall(r"""<(?:script|link|img|iframe|source)\b[^>]*?\b(?:src|href)=["'](https?://[^"']+)""", html))
    urls |= set(re.findall(r"""(?:url\(|@import\s+|fetch\(|import\()\s*["']?(https?://[^"')\s]+)""", html))
    assert urls == {CHART_URL}, urls  # links a visitor may follow (<a href>) are not loads
    tag = re.search(r"<script[^>]+" + re.escape(CHART_URL) + r"[^>]*>", html).group(0)
    digest = base64.b64encode(hashlib.sha512(CHART_JS.read_bytes()).digest()).decode()
    assert f'integrity="sha512-{digest}"' in tag, "testdata's Chart.js is not the file the page pins"
