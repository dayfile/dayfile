"""The dayfile.app web app: public pages, the read-token sign-in, and the log viewer."""

import hashlib
import html
import http.server
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.auth.tokens import create_token, revoke_token
from dayfile.db import connect
from dayfile.tenancy import DirectoryTenancy
from dayfile.web import client as web_client
from dayfile.web import create_web_app
from dayfile.web.client import ApiResult, http_fetch
from dayfile.web.creds import parse_credentials
from dayfile.web.present import safe_href

BASE = "example.test"
ORIGIN = "https://dayfile.app"
COOKIE = "__Host-dayfile"
HTMX_SHA256 = "e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447"


class Backend:
    """A real dayfile API over a temporary tenant directory, reached the way the web app does."""

    def __init__(self, tmp_path):
        self.tenancy = DirectoryTenancy(tmp_path / "tenants", BASE)
        self.tenancy.create("acme")
        self.now = "2026-11-06T12:00:00Z"
        self.api = TestClient(
            create_app(tenancy=self.tenancy, read_limit=(10000, 60), clock=lambda: self.now)
        )
        self.calls: list[tuple[str, str, list[tuple[str, str]]]] = []
        conn = connect(self.tenancy.get("acme").db_path)
        try:
            self.read = create_token(conn, ["self"]).value
            self.write = create_token(conn, ["self"], write_sources=["web-test"]).value
        finally:
            conn.close()
        self.counter = 0

    def fetch(self, slug, token, params):
        self.calls.append((slug, token, list(params)))
        r = self.api.get(
            "/events",
            params=list(params),
            headers={"Authorization": f"Bearer {token}", "Host": f"{slug}.{BASE}"},
        )
        body = (
            r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        )
        return ApiResult(r.status_code, body)

    def post(self, **fields):
        self.counter += 1
        event = {
            "specversion": "dayfile/1",
            "id": f"01JC8Z4Q7M2X9K3T5W6N8R{self.counter:04d}",
            "source": {"id": "web-test", "ref": str(self.counter)},
            "occurred_at": "2026-11-06T10:00:00Z",
            "title": f"event {self.counter}",
            **fields,
        }
        r = self.api.post(
            "/events",
            json=event,
            headers={"Authorization": f"Bearer {self.write}", "Host": f"acme.{BASE}"},
        )
        assert r.status_code in (200, 201), r.text
        return event["id"]

    def revoke_reader(self):
        conn = connect(self.tenancy.get("acme").db_path)
        try:
            revoke_token(conn, self.read.split(".")[0], "2026-11-07T00:00:00Z")
        finally:
            conn.close()


@pytest.fixture
def backend(tmp_path):
    return Backend(tmp_path)


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def make_web(backend, **kw):
    kw.setdefault("monotonic", Clock())
    kw.setdefault("probe", lambda: "ok")
    return TestClient(
        create_web_app(fetch=backend.fetch, base_domain=BASE, site_origin=ORIGIN, **kw),
        base_url=ORIGIN,
    )


@pytest.fixture
def web(backend):
    return make_web(backend)


def sign_in(web, backend, token=None):
    r = web.post(
        "/login",
        data={"slug": "acme", "token": token or backend.read},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return r


def poller_url(page):
    return html.unescape(re.search(r'hx-get="([^"]+)"', page).group(1))


# --- public pages and headers ---------------------------------------------------------

PUBLIC = ["/", "/docs", "/status", "/pricing", "/terms", "/privacy", "/login"]


@pytest.mark.parametrize("path", PUBLIC)
def test_public_pages_render(web, path):
    r = web.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<title>" in r.text and 'rel="icon"' in r.text


@pytest.mark.parametrize("path", [*PUBLIC, "/nope", "/static/site.css"])
def test_every_response_carries_the_security_headers(web, path):
    h = web.get(path).headers
    csp = h["content-security-policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"
    assert h["x-frame-options"] == "DENY"


@pytest.mark.parametrize("path", PUBLIC)
def test_pages_use_no_inline_script_or_style_and_load_nothing_from_elsewhere(web, path):
    page = web.get(path).text
    assert "<style" not in page and " style=" not in page
    assert not re.search(r"<script(?![^>]*\bsrc=)", page)
    # Nothing is loaded from elsewhere; a plain link (the footer's GitHub link) is only a link.
    for url in re.findall(r"""(?:\bsrc=|<link[^>]*\bhref=)["'](https?://[^"']+)""", page):
        pytest.fail(f"{path} references an external resource: {url}")
    for url in re.findall(r"""<a [^>]*href=["'](https?://[^"']+)""", page):
        assert url == "https://github.com/dayfile/dayfile", f"{path} links to {url}"


@pytest.mark.parametrize("path", [*PUBLIC, "/healthz", "/robots.txt"])
def test_head_requests_work_for_monitors_and_link_checkers(web, path):
    r = web.head(path)
    assert r.status_code == 200 and r.content == b""


def test_unknown_paths_get_an_html_404(web):
    r = web.get("/nope")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")


def test_public_pages_are_cacheable_and_private_ones_are_not(web):
    home = web.get("/").headers
    assert "max-age" in home["cache-control"] and home["cache-control"].startswith("private")
    assert "cookie" in home["vary"].lower()
    assert web.get("/login").headers["cache-control"] == "no-store"


def test_the_icon_and_stylesheet_are_served(web):
    assert web.get("/favicon.svg").headers["content-type"].startswith("image/svg+xml")
    assert web.get("/favicon.ico").content[:4] == b"\x00\x00\x01\x00"
    css = web.get("/static/site.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "max-age=604800" in css.headers["cache-control"]  # kept for a week by browsers


def test_the_vendored_htmx_is_the_pinned_file():
    path = Path(__file__).parents[1] / "src" / "dayfile" / "web" / "static" / "htmx.min.js"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == HTMX_SHA256


def test_healthz_needs_nothing(web):
    r = web.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_status_page_reports_the_api(backend):
    up = make_web(backend).get("/status")
    assert up.status_code == 200 and "All systems operational" in up.text
    down = make_web(backend, probe=lambda: "unreachable").get("/status")
    assert down.status_code == 503 and "Something is down" in down.text


def test_a_long_body_is_clamped_behind_read_more_and_a_short_one_is_not(backend):
    long_id = backend.post(body="word " * 200)
    lines_id = backend.post(body="\n".join(f"line {i}" for i in range(15)))
    short_id = backend.post(body="a short note")
    web = make_web(backend)
    sign_in(web, backend)
    text = web.get("/log").text
    for eid in (long_id, lines_id):
        assert f'id="more-{eid}"' in text and f'for="more-{eid}"' in text
    assert f"more-{short_id}" not in text
    assert text.count("Read more") == 2 and "word word" in text  # the whole text is still there


def test_robots_keeps_the_signed_in_pages_out_of_search(web):
    text = web.get("/robots.txt").text
    assert "Disallow: /log" in text and "Disallow: /login" in text


def test_the_legal_pages_say_they_are_drafts(web):
    for path in ("/terms", "/privacy"):
        assert "draft" in web.get(path).text.lower()


# --- credentials parsing --------------------------------------------------------------

TOK = "0123456789ab." + "A" * 43


@pytest.mark.parametrize(
    "slug, token, want",
    [
        ("acme", TOK, ("acme", TOK)),
        ("  ACME ", f" {TOK} ", ("acme", TOK)),
        ("acme.example.test", TOK, ("acme", TOK)),
        ("", f"https://acme.example.test/t/{TOK}/feed.json", ("acme", TOK)),
        ("", f"https://acme.example.test/t/{TOK}/feed.atom", ("acme", TOK)),
        ("acme", f"https://acme.example.test/t/{TOK}/feed.json", ("acme", TOK)),
    ],
)
def test_parse_credentials_accepts_the_forms_people_paste(slug, token, want):
    assert parse_credentials(slug, token, BASE) == want


@pytest.mark.parametrize(
    "slug, token",
    [
        ("", ""),
        ("acme", ""),
        ("", TOK),
        ("../evil", TOK),
        ("evil.com", TOK),
        ("acme/../x", TOK),
        ("-acme", TOK),
        ("a" * 40, TOK),
        ("www", "nonsense"),
        ("acme", "short"),
        ("acme", TOK[:20] + "\n" + TOK[20:]),
        ("acme", "0123456789ab.AAAA AAAA"),
        ("", f"https://evil.test/t/{TOK}/feed.json"),
        ("", f"https://acme.example.test.evil.test/t/{TOK}/feed.json"),
        ("", f"https://acme.example.test/other/{TOK}/feed.json"),
        ("", f"http://acme.example.test:8080@evil.test/t/{TOK}/feed.json"),
    ],
)
def test_parse_credentials_refuses_everything_else(slug, token):
    assert parse_credentials(slug, token, BASE) is None


# --- links in untrusted events --------------------------------------------------------


@pytest.mark.parametrize(
    "href, ok",
    [
        ("https://example.com/a?b=c", True),
        ("http://example.com", True),
        ("mailto:me@example.com", True),
        ("javascript:alert(1)", False),
        ("JaVaScRiPt:alert(1)", False),
        (" javascript:alert(1)", False),
        ("java\tscript:alert(1)", False),
        ("data:text/html,<script>alert(1)</script>", False),
        ("vbscript:x", False),
        ("//evil.test/x", False),
        ("/relative", False),
        ("https:///nohost", False),
        ("", False),
        ("https://exa mple.com", False),
    ],
)
def test_safe_href_allows_only_plain_web_and_mail_links(href, ok):
    assert (safe_href(href) is not None) == ok


# --- sign in --------------------------------------------------------------------------


def test_signing_in_sets_a_locked_down_cookie_and_goes_to_the_log(web, backend):
    r = sign_in(web, backend)
    assert r.headers["location"] == "/log"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=acme:{backend.read}")
    low = cookie.lower()
    assert "httponly" in low and "secure" in low and "samesite=strict" in low
    assert "path=/" in low and "domain" not in low
    assert backend.calls[-1][0] == "acme"


def test_the_token_never_appears_in_any_page_after_sign_in(web, backend):
    sign_in(web, backend)
    secret = backend.read.split(".")[1]
    for path in ("/log", "/", "/login", "/docs"):
        assert secret not in web.get(path).text


def test_a_pasted_feed_url_signs_in_too(web, backend):
    url = f"https://acme.{BASE}/t/{backend.read}/feed.json"
    r = web.post(
        "/login",
        data={"slug": "", "token": url},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert backend.calls[-1][:2] == ("acme", backend.read)


def test_a_wrong_token_is_refused_without_a_cookie_and_without_detail(web, backend):
    bad = "0123456789ab." + "B" * 43
    r = web.post("/login", data={"slug": "acme", "token": bad}, headers={"Origin": ORIGIN})
    assert r.status_code == 401
    assert "set-cookie" not in r.headers
    assert "don" in r.text and "match" in r.text
    assert bad not in r.text  # the form does not echo the secret back


def test_an_unknown_log_gets_the_same_answer_as_a_wrong_token(web, backend):
    a = web.post(
        "/login", data={"slug": "nosuch", "token": backend.read}, headers={"Origin": ORIGIN}
    )
    b = web.post(
        "/login",
        data={"slug": "acme", "token": "0123456789ab." + "B" * 43},
        headers={"Origin": ORIGIN},
    )
    assert a.status_code == b.status_code == 401

    def shape(text):  # the form echoes the log name that was typed, nothing else differs
        return re.sub(r"\s+", " ", re.sub(r'value="[^"]*"', "", text))

    assert shape(a.text) == shape(b.text)


def test_a_write_token_is_told_it_cannot_read(web, backend):
    r = web.post(
        "/login", data={"slug": "acme", "token": backend.write}, headers={"Origin": ORIGIN}
    )
    assert r.status_code == 403
    assert "read token" in r.text
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize(
    "data", [{}, {"slug": "acme"}, {"slug": "../x", "token": TOK}, {"slug": "acme", "token": "x"}]
)
def test_malformed_sign_in_never_reaches_the_api(web, backend, data):
    r = web.post("/login", data=data, headers={"Origin": ORIGIN})
    assert r.status_code in (400, 422)
    assert backend.calls == []


@pytest.mark.parametrize(
    "headers",
    [{}, {"Origin": "https://evil.test"}, {"Origin": "null"}, {"Origin": "http://dayfile.app"}],
)
def test_sign_in_from_another_site_is_refused(web, backend, headers):
    r = web.post("/login", data={"slug": "acme", "token": backend.read}, headers=headers)
    assert r.status_code == 403
    assert backend.calls == []
    assert "set-cookie" not in r.headers


def test_sign_in_attempts_are_limited_for_the_whole_process(backend):
    clock = Clock()
    web = make_web(backend, login_limit=(3, 60), monotonic=clock)
    bad = {"slug": "acme", "token": "0123456789ab." + "B" * 43}
    codes = [web.post("/login", data=bad, headers={"Origin": ORIGIN}).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    assert len(backend.calls) == 3  # throttled attempts never reach the API
    clock.t += 61
    assert web.post("/login", data=bad, headers={"Origin": ORIGIN}).status_code == 401


def test_an_unreachable_api_is_a_503_not_a_crash(backend):
    web = make_web(backend)
    web.app.state.fetch = lambda *a: ApiResult(0)
    r = web.post("/login", data={"slug": "acme", "token": backend.read}, headers={"Origin": ORIGIN})
    assert r.status_code == 503 and "set-cookie" not in r.headers


def test_signing_out_clears_the_cookie_and_needs_a_same_origin_post(web, backend):
    sign_in(web, backend)
    assert web.post("/logout", headers={"Origin": "https://evil.test"}).status_code == 403
    assert web.get("/log").status_code == 200  # still signed in
    r = web.post("/logout", headers={"Origin": ORIGIN}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "max-age=0" in r.headers["set-cookie"].lower()
    assert web.get("/log", follow_redirects=False).status_code == 303


# --- the log viewer -------------------------------------------------------------------


def test_the_log_needs_a_session(web, backend):
    r = web.get("/log", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert backend.calls == []


def test_the_log_lists_newest_first_with_what_matters(web, backend):
    backend.post(title="first", tags=["work", "plan"], body="line one\nline two", urgency=2)
    backend.post(title="second", tags=["todo"])
    sign_in(web, backend)
    page = web.get("/log").text
    assert page.index("<h3>second</h3>") < page.index("<h3>first</h3>")
    assert "work" in page and "plan" in page and "todo" in page
    assert "line one" in page
    assert "web-test" in page  # the source
    assert web.get("/log").headers["cache-control"] == "no-store"


def test_hostile_event_text_is_escaped_everywhere(web, backend):
    backend.post(
        title="<script>alert(1)</script>",
        body="<img src=x onerror=alert(1)>",
        payload={"k": "</details><script>alert(2)</script>"},
    )
    sign_in(web, backend)
    page = web.get("/log").text
    for needle in ("<script>alert", "<img src=x", "<i>", "</details><script>"):
        assert needle not in page
    assert "&lt;script&gt;alert(1)" in page


def test_only_safe_links_become_links(web, backend):
    backend.post(
        title="links",
        links=[
            {"rel": "related", "href": "javascript:alert(1)"},
            {"rel": "related", "href": "https://example.com/ok"},
            {"rel": "related", "href": "data:text/html,x"},
        ],
    )
    sign_in(web, backend)
    page = web.get("/log").text
    assert 'href="https://example.com/ok"' in page
    assert "noopener" in page and "noreferrer" in page
    assert 'href="javascript' not in page.lower() and 'href="data:' not in page.lower()
    assert "javascript:alert(1)" in page  # shown as inert text, not hidden from the reader


def test_lifecycle_state_is_shown(web, backend):
    target = backend.post(title="buy milk", tags=["todo"])
    backend.post(title="Done: buy milk", refs=[{"rel": "completes", "id": target}])
    sign_in(web, backend)
    page = web.get("/log").text
    todo = re.search(
        r'class="event status-(\w+)">(?:(?!</article>).)*?<h3>buy milk</h3>', page, re.S
    )
    assert todo and todo.group(1) == "closed"


def test_filters_are_forwarded_and_validated(web, backend):
    sign_in(web, backend)
    web.get("/log?tag=work&status=all&limit=100")
    params = backend.calls[-1][2]
    assert ("tag", "work") in params and ("limit", "100") in params
    assert sorted(v for k, v in params if k == "status") == [
        "closed",
        "none",
        "open",
        "retracted",
        "superseded",
    ]
    web.get("/log?limit=99999&status=bogus&tag=Bad!")
    params = backend.calls[-1][2]
    assert ("limit", "50") in params
    assert not [1 for k, _ in params if k in ("tag", "status")]


def test_a_bad_filter_is_reported_not_silently_dropped(web, backend):
    sign_in(web, backend)
    assert "filter" in web.get("/log?tag=Bad!").text.lower()


def test_a_revoked_token_ends_the_session(web, backend):
    sign_in(web, backend)
    backend.revoke_reader()
    r = web.get("/log", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?expired=1"
    assert "max-age=0" in r.headers["set-cookie"].lower()
    assert "expired" in web.get("/login?expired=1").text.lower()


def test_a_tampered_cookie_is_ignored_without_calling_the_api(web, backend):
    for value in ("../evil:" + TOK, "acme:nonsense", "evil.com:" + TOK, "", "acme", ":::"):
        web.cookies.set(COOKIE, value)
        assert web.get("/log", follow_redirects=False).status_code == 303
    assert backend.calls == []


def test_rate_limiting_by_the_api_is_shown_as_slow_down(web, backend):
    sign_in(web, backend)
    web.app.state.fetch = lambda *a: ApiResult(429, {"error": "read rate limit exceeded"})
    r = web.get("/log")
    assert r.status_code == 429 and "slow" in r.text.lower()


def test_an_unreachable_api_shows_a_503(web, backend):
    sign_in(web, backend)
    web.app.state.fetch = lambda *a: ApiResult(0)
    assert web.get("/log").status_code == 503


# --- live updates ---------------------------------------------------------------------


def test_the_log_page_polls_for_what_comes_next(web, backend):
    backend.post(title="old")
    sign_in(web, backend)
    page = web.get("/log?tag=").text
    assert 'src="/static/htmx.min.js"' in page
    assert 'hx-trigger="every 5s"' in page and "htmx-config" in page
    url = poller_url(page)
    assert url.startswith("/log/new?") and "after=1" in url

    backend.post(title="arrived later")
    r = web.get(url)
    assert r.status_code == 200
    assert "arrived later" in r.text and "old" not in r.text
    follow = poller_url(r.text)
    assert "after=2" in follow
    assert "arrived later" not in web.get(follow).text  # exactly once


def test_polling_keeps_the_filters(web, backend):
    backend.post(title="a", tags=["x"])
    sign_in(web, backend)
    url = poller_url(web.get("/log?tag=x").text)
    assert "tag=x" in url
    backend.post(title="b", tags=["y"])
    backend.post(title="c", tags=["x"])
    text = web.get(url).text
    assert re.search(r">\s*c\s*<", text) and not re.search(r">\s*b\s*<", text)


def test_a_late_release_arrives_through_polling(web, backend):
    backend.post(title="held back", visible_at="2026-11-06T13:00:00Z")  # seq 1
    backend.post(title="visible now")  # seq 2
    sign_in(web, backend)
    page = web.get("/log").text
    assert "visible now" in page and "held back" not in page
    url = poller_url(page)
    assert "after=2" in url and "released=2026-11-06T12%3A00%3A00Z" in url

    backend.now = "2026-11-06T13:30:00Z"  # the held event's time has come, below the cursor
    r = web.get(url)
    assert "held back" in r.text and "visible now" not in r.text
    assert "held back" not in web.get(poller_url(r.text)).text  # and only once


def test_sign_in_page_sends_a_visitor_with_a_read_session_to_the_log(web, backend):
    sign_in(web, backend)
    r = web.get("/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/log"
    assert web.get("/login?expired=1").status_code == 200  # the way out of a dead session


def test_polling_after_the_session_ends_redirects_the_browser_to_sign_in(web, backend):
    backend.post(title="x")
    sign_in(web, backend)
    url = poller_url(web.get("/log").text)
    backend.revoke_reader()
    r = web.get(url)
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login?expired=1"


def test_a_failed_poll_keeps_polling_with_the_same_cursor(web, backend):
    backend.post(title="x")
    sign_in(web, backend)
    url = poller_url(web.get("/log").text)
    web.app.state.fetch = lambda *a: ApiResult(0)
    r = web.get(url)
    assert r.status_code == 200 and poller_url(r.text) == url


@pytest.mark.parametrize(
    "query", ["after=x", "after=-1", "after=1&released=yesterday", "after=1&released=2026-11-06"]
)
def test_poll_cursors_are_validated(web, backend, query):
    sign_in(web, backend)
    n = len(backend.calls)
    assert web.get(f"/log/new?{query}").status_code == 400
    assert len(backend.calls) == n


def test_polling_needs_a_session(web):
    assert web.get("/log/new?after=0", follow_redirects=False).status_code in (303, 401)


# --- the one outgoing request ---------------------------------------------------------


class Stub:
    """A tiny HTTP server standing in for the dayfile API."""

    def __init__(self, status=200, body=b'{"events": []}', delay=0.0, headers=None):
        outer = self
        self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen.append((self.path, dict(self.headers)))
                time.sleep(delay)
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    made = []

    def make(**kw):
        s = Stub(**kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def test_the_fetcher_names_the_tenant_by_host_and_sends_the_bearer(stub):
    s = stub()
    res = http_fetch(s.url, BASE)("acme", TOK, [("limit", "5"), ("tag", "a b")])
    assert res.status == 200 and res.body == {"events": []}
    path, headers = s.seen[0]
    assert path == "/events?limit=5&tag=a+b"
    assert headers["Host"] == f"acme.{BASE}"
    assert headers["Authorization"] == f"Bearer {TOK}"


def test_the_fetcher_reports_statuses_and_never_raises(stub):
    assert (
        http_fetch(stub(status=401, body=b'{"error": "x"}').url, BASE)("a", TOK, []).status == 401
    )
    assert http_fetch(stub(status=500, body=b"boom").url, BASE)("a", TOK, []) == ApiResult(
        500, None
    )
    assert http_fetch(stub(body=b"not json").url, BASE)("a", TOK, []) == ApiResult(200, None)
    assert http_fetch("http://127.0.0.1:9", BASE)("a", TOK, []) == ApiResult(0)


def test_the_fetcher_does_not_follow_redirects(stub):
    other = stub(body=b'{"events": ["leaked"]}')
    s = stub(status=302, body=b"", headers={"Location": other.url + "/events"})
    res = http_fetch(s.url, BASE)("a", TOK, [])
    assert res.status == 302 and res.body is None
    assert other.seen == []


def test_the_fetcher_gives_up_on_a_slow_api(stub):
    s = stub(delay=0.6)
    assert http_fetch(s.url, BASE, timeout=0.1)("a", TOK, []) == ApiResult(0)


def test_the_fetcher_refuses_an_oversized_answer(stub, monkeypatch):
    monkeypatch.setattr(web_client, "MAX_BODY", 100)
    s = stub(body=b'{"events": [' + b'"x",' * 100 + b'"y"]}')
    assert http_fetch(s.url, BASE)("a", TOK, []) == ApiResult(0)


def test_fonts_are_bundled_served_from_this_origin_and_allowed_by_the_policy(web):
    for name in ("source-sans-3-variable-latin.woff2", "jetbrains-mono-variable-latin.woff2"):
        r = web.get(f"/static/fonts/{name}")
        assert r.status_code == 200 and r.content[:4] == b"wOF2"
        assert "max-age" in r.headers["cache-control"]
    csp = web.get("/").headers["content-security-policy"]
    assert "font-src 'self'" in csp and "fonts.googleapis" not in csp
    page = web.get("/").text
    assert "fonts.googleapis" not in page and "fonts.gstatic" not in page


def test_font_licences_travel_with_the_font_files():
    from pathlib import Path

    fonts = Path(__file__).parents[1] / "src/dayfile/web/static/fonts"
    notice = (fonts / "fonts.NOTICE").read_text()
    assert "SIL Open Font License" in notice and list(fonts.glob("*.woff2"))


def test_a_stale_session_cookie_still_gets_the_home_page_not_a_detour(web):
    assert web.get("/", follow_redirects=False).status_code == 200
    web.cookies.set("__Host-dayfile-session", "x")  # even a stale session shows the page
    r = web.get("/", follow_redirects=False)
    assert r.status_code == 200 and 'href="/login">Sign in' in r.text
    web.cookies.clear()
    web.cookies.set(COOKIE, "acme:0123456789ab." + "A" * 43)
    r = web.get("/", follow_redirects=False)
    assert r.status_code == 200 and 'href="/log">Open your log' in r.text


def test_the_header_follows_the_visitors_state(web):
    assert 'href="/login">Sign in' in web.get("/").text
    web.cookies.set(COOKIE, "acme:0123456789ab." + "A" * 43)
    page = web.get("/terms").text
    assert 'href="/log">Your log' in page and "Sign in" not in page
    web.cookies.clear()
    web.cookies.set("__Host-dayfile-session", "x")
    page = web.get("/docs").text  # a session cookie nobody honors is signed out
    assert 'href="/login">Sign in' in page and 'href="/account/settings"' not in page
    assert page.count('href="/docs"') == 1 and page.index('href="/docs"') > page.index("</main>")


def test_the_log_token_feed_diagram_is_at_the_top_of_the_docs_and_on_the_intro_page(web):
    docs = web.get("/docs").text
    assert "<svg" in docs and docs.index("<svg") < docs.index("The ideas")
    assert "<svg" in web.get("/").text
    assert "style=" not in docs and "<script" not in docs


def test_the_admin_sender_keeps_a_gzip_download_as_bytes_and_caps_it(stub, monkeypatch):
    from dayfile.web.accounts import control as control_mod

    payload = b"\x1f\x8b" + b"x" * 5000  # bigger than a JSON answer may be
    s = stub(body=payload, headers={"Content-Type": "application/gzip"})
    monkeypatch.setattr(control_mod, "MAX_BODY", 100)
    got = control_mod.http_send(s.url, "secret")("GET", "/internal/admin/tenants/a/export")
    assert got == ApiResult(200, payload)
    monkeypatch.setattr(control_mod, "EXPORT_MAX", 100)
    big = control_mod.http_send(s.url, "secret")("GET", "/internal/admin/tenants/a/export")
    assert big == ApiResult(0)


def test_several_tags_in_the_filter_all_have_to_match(web, backend):
    sign_in(web, backend)
    page = web.get("/log?tag=work, launch  work").text
    tags = [v for k, v in backend.calls[-1][2] if k == "tag"]
    assert tags == ["work", "launch"] and 'value="work launch"' in page
    web.get("/log?tag=a b c d e f")  # more than five is dropped, and said so
    assert not [1 for k, _ in backend.calls[-1][2] if k == "tag"]


def test_the_demo_is_a_filterable_example_log_for_anyone(web):
    page = web.get("/demo")
    assert page.status_code == 200 and "Sample data" in page.text
    assert "Nightly backup finished" in page.text and "Disk at 82%" in page.text
    ops = web.get("/demo?tag=ops").text
    assert "Deployed v1.8.2" in ops and "Nightly backup" not in ops
    open_only = web.get("/demo?status=open").text
    assert "Renew the domain" in open_only and "Rotate the API keys" not in open_only
    assert (
        "Nothing" not in web.get("/demo?tag=ops tls").text
        or "No sample" in web.get("/demo?tag=ops tls").text
    )


def test_the_landing_page_leads_with_the_outcome_and_links_the_example(web):
    page = web.get("/").text
    assert 'One <span class="grad">log</span> for your scripts' in page and 'href="/demo"' in page
    assert "Your data, your control" in page
    assert "Scheduled events" in page and "Time release" not in page
    assert "hidden from dayfile's normal views, but the record stays" in page
    assert "Why not just a file" in page and "50,000 events" in page


def test_security_txt_names_a_contact_and_is_not_about_to_expire(web):
    from datetime import UTC, datetime, timedelta

    from dayfile.web.app import SECURITY_TXT_EXPIRES

    r = web.get("/.well-known/security.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "Contact: mailto:team@dayfile.app" in r.text and SECURITY_TXT_EXPIRES in r.text
    expires = datetime.strptime(SECURITY_TXT_EXPIRES, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert expires - datetime.now(UTC) > timedelta(days=30), "renew security.txt"


def test_sign_in_page_has_no_redundant_sign_in_link(web):
    assert 'class="signin"' not in web.get("/login").text
    assert 'class="signin"' in web.get("/docs").text
