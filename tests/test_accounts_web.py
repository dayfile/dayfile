"""Sign in with GitHub, invite signup and sessions on the web app (M16c), against a stub GitHub."""

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from dayfile.web.accounts import github, store
from dayfile.web.app import create_web_app

ORIGIN = "https://dayfile.app"
NOW = ["2026-11-10T09:00:00Z"]


class StubGitHub:
    """A GitHub that knows one identity per code, and records what it was asked."""

    def __init__(self):
        self.people = {"code-ann": (101, "ann"), "code-bob": (202, "bob")}
        self.calls = []

    def authorize_url(self, state, verifier):
        return f"https://github.test/authorize?state={state}&challenge={github.challenge(verifier)}"

    def identify(self, code, verifier):
        self.calls.append((code, verifier))
        if code not in self.people:
            raise github.GitHubError("no")
        return self.people[code]


@pytest.fixture
def gh():
    return StubGitHub()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "accounts.db"


@pytest.fixture
def web(db, gh):
    app = create_web_app(
        accounts_db=db,
        github=gh,
        secure_cookies=False,
        clock=lambda: NOW[0],
        base_domain="example.test",
        site_origin=ORIGIN,
        fetch=lambda *a: None,
    )
    return TestClient(app, base_url=ORIGIN, follow_redirects=False)


def invite(db, **kw):
    conn = store.connect(db)
    try:
        return store.create_invite(conn, note="t", now=NOW[0], **kw)
    finally:
        conn.close()


def sign_in(web, code="code-ann"):
    r = web.get("/auth/github")
    assert r.status_code == 302
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    return web.get("/auth/github/callback", params={"code": code, "state": state})


def create_account(web, db, code="code-ann"):
    sign_in(web, code)
    r = web.post("/signup", data={"invite": invite(db), "terms": "yes"}, headers={"Origin": ORIGIN})
    assert r.status_code == 303, r.text
    return r


def test_the_account_bar_shows_only_for_a_live_session(web, db):
    web.cookies.set("__Host-dayfile-session", "stale")
    page = web.get("/docs").text
    assert 'href="/login">Sign in' in page and 'href="/account/settings"' not in page
    web.cookies.clear()
    create_account(web, db)
    page = web.get("/docs").text
    assert 'href="/account/settings"' in page and ">Sign in<" not in page


# --- the redirect to GitHub ---


def test_the_sign_in_link_goes_to_github_with_state_and_a_pkce_challenge(web):
    r = web.get("/auth/github")
    assert r.status_code == 302 and r.headers["location"].startswith("https://github.test/")
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert len(query["state"][0]) >= 40 and len(query["challenge"][0]) >= 40
    cookie = r.headers["set-cookie"].lower()
    assert "dayfile-oauth=" in cookie and "httponly" in cookie and "samesite=lax" in cookie


def test_the_login_page_offers_github_only_when_it_is_configured(web, tmp_path):
    assert "Sign in with GitHub" in web.get("/login").text
    plain = TestClient(create_web_app(secure_cookies=False, fetch=lambda *a: None))
    assert "Sign in with GitHub" not in plain.get("/login").text
    assert plain.get("/auth/github", follow_redirects=False).status_code == 404


# --- the callback ---


def test_a_callback_without_the_matching_state_cookie_is_refused(web, gh):
    state = parse_qs(urlsplit(web.get("/auth/github").headers["location"]).query)["state"][0]
    web.cookies.clear()
    r = web.get("/auth/github/callback", params={"code": "code-ann", "state": state})
    assert r.status_code == 400 and gh.calls == []


def test_a_state_works_exactly_once(web):
    state = parse_qs(urlsplit(web.get("/auth/github").headers["location"]).query)["state"][0]
    first = web.get("/auth/github/callback", params={"code": "code-ann", "state": state})
    assert first.status_code == 200
    web.cookies.set("dayfile-oauth", state)  # replay with a matching cookie
    again = web.get("/auth/github/callback", params={"code": "code-ann", "state": state})
    assert again.status_code == 400


def test_an_expired_state_is_refused(web):
    state = parse_qs(urlsplit(web.get("/auth/github").headers["location"]).query)["state"][0]
    NOW[0] = "2026-11-10T09:11:00Z"
    try:
        r = web.get("/auth/github/callback", params={"code": "code-ann", "state": state})
        assert r.status_code == 400
    finally:
        NOW[0] = "2026-11-10T09:00:00Z"


def test_the_verifier_reaches_github_and_a_failed_exchange_is_a_clean_error(web, gh):
    r = sign_in(web, "nonsense")
    assert r.status_code == 502 and "nonsense" not in r.text
    assert len(gh.calls[0][1]) >= 40


def test_cancelling_on_github_ends_cleanly(web):
    r = web.get("/auth/github/callback", params={"error": "access_denied", "state": "x"})
    assert r.status_code == 400 and "dayfile-oauth" in r.headers["set-cookie"]


def test_the_callback_answers_with_a_page_not_a_redirect(web):
    r = sign_in(web)  # a SameSite=Strict cookie would be lost on a redirect chain from GitHub
    assert r.status_code == 200 and 'href="/signup"' in r.text
    assert "dayfile-session=" in r.headers["set-cookie"]
    assert "samesite=lax" in r.headers["set-cookie"].lower()


# --- invite signup ---


def test_signup_needs_a_valid_invite_and_then_signs_in_as_the_new_account(web, db):
    sign_in(web)
    page = web.get("/signup").text
    assert 'name="invite"' in page and 'class="signin"' not in page
    r = web.post("/signup", data={"invite": invite(db), "terms": "yes"}, headers={"Origin": ORIGIN})
    assert r.status_code == 303 and r.headers["location"] == "/account"
    page = web.get("/account")
    assert page.status_code == 200 and "Hello, ann" in page.text
    conn = store.connect(db)
    account = store.get_account(conn, github_id=101)
    assert account["terms_version"] == store.TERMS_VERSION and account["terms_accepted_at"]


def test_a_bad_invite_creates_nothing_and_says_so_plainly(web, db):
    sign_in(web)
    r = web.post(
        "/signup", data={"invite": "AAAA-AAAA-AAAA", "terms": "yes"}, headers={"Origin": ORIGIN}
    )
    assert r.status_code == 400 and "not valid" in r.text
    assert store.get_account(store.connect(db), github_id=101) is None


def test_the_terms_must_be_accepted(web, db):
    sign_in(web)
    r = web.post("/signup", data={"invite": invite(db)}, headers={"Origin": ORIGIN})
    assert r.status_code == 400 and "terms" in r.text.lower()
    assert store.get_account(store.connect(db), github_id=101) is None


def test_signup_needs_the_same_origin(web, db):
    sign_in(web)
    code = invite(db)
    assert web.post("/signup", data={"invite": code, "terms": "yes"}).status_code == 403
    r = web.post(
        "/signup", data={"invite": code, "terms": "yes"}, headers={"Origin": "https://evil.test"}
    )
    assert r.status_code == 403
    assert store.get_account(store.connect(db), github_id=101) is None


def test_guessing_codes_is_throttled(web):
    sign_in(web)
    codes = [
        web.post(
            "/signup",
            data={"invite": f"AAAA-AAAA-AAA{i}", "terms": "yes"},
            headers={"Origin": ORIGIN},
        ).status_code
        for i in range(7)
    ]
    assert codes[:5] == [400] * 5 and codes[5:] == [429, 429]


def test_pages_send_visitors_to_the_right_place(web, db):
    assert web.get("/signup").headers["location"] == "/login"
    assert web.get("/account").headers["location"] == "/login"
    sign_in(web)
    assert web.get("/account").headers["location"] == "/signup"  # pending, no account yet
    create_account(TestClient(web.app, base_url=ORIGIN, follow_redirects=False), db, "code-bob")


def test_a_returning_account_goes_straight_to_its_page(web, db):
    create_account(web, db)
    web.cookies.clear()
    r = sign_in(web)
    assert r.status_code == 200 and 'href="/account"' in r.text


def test_signing_out_ends_the_session_on_the_server(web, db):
    create_account(web, db)
    raw = web.cookies.get("dayfile-session")
    assert web.post("/account/logout", headers={"Origin": ORIGIN}).status_code == 303
    web.cookies.set("dayfile-session", raw)  # the old cookie must be dead, not just removed
    assert web.get("/account").status_code == 303
    assert web.post("/account/logout").status_code == 403


def test_signing_out_also_clears_the_log_viewer_cookies(web, db):
    create_account(web, db)
    web.cookies.set("dayfile", "abc:" + "x" * 43)
    web.cookies.set("dayfile-post", "abc:" + "x" * 43)
    r = web.post("/account/logout", headers={"Origin": ORIGIN})
    assert r.status_code == 303
    cleared = " ".join(r.headers.get_list("set-cookie"))
    for name in ("dayfile-session", "dayfile=", "dayfile-post"):
        assert f"{name}" in cleared and "Max-Age=0" in cleared


def test_sessions_idle_out_and_expire(web, db):
    create_account(web, db)
    NOW[0] = "2026-11-10T17:30:00Z"  # idle for more than 8 hours
    try:
        assert web.get("/account").headers["location"] == "/login"
    finally:
        NOW[0] = "2026-11-10T09:00:00Z"


def test_a_suspended_account_cannot_sign_in_or_keep_a_session(web, db):
    create_account(web, db)
    conn = store.connect(db)
    store.set_status(conn, "ann", "suspended", NOW[0])
    assert web.get("/account").headers["location"] == "/login"
    web.cookies.clear()
    assert sign_in(web).status_code == 403


def test_the_session_cookie_is_opaque_and_only_its_hash_is_stored(web, db):
    create_account(web, db)
    raw = web.cookies.get("dayfile-session")
    conn = store.connect(db)
    dump = " ".join(str(v) for row in conn.execute("SELECT * FROM session") for v in row)
    assert raw not in dump and "ann" not in raw


def test_account_pages_are_never_cached_and_keep_the_security_headers(web, db):
    create_account(web, db)
    r = web.get("/account")
    assert r.headers["cache-control"] == "no-store"
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "style=" not in web.get("/signup").text  # inline styles are blocked by the CSP


def test_no_secret_reaches_the_pages_or_the_database(web, db, gh):
    create_account(web, db)
    conn = store.connect(db)
    everything = " ".join(
        str(v)
        for table in ("account", "session", "oauth", "audit")
        for row in conn.execute(f"SELECT * FROM {table}")
        for v in row
    )
    assert "access_token" not in everything and "secret" not in everything.lower()


# --- the GitHub client itself ---


def test_the_client_exchanges_the_code_and_reads_only_id_and_login():
    sent = []

    def transport(method, url, headers, body):
        sent.append((method, url, headers, body))
        if url == github.TOKEN_URL:
            return 200, b'{"access_token": "gho_x", "token_type": "bearer"}'
        return 200, b'{"id": 7, "login": "ann", "email": "private@example.test", "name": "Ann"}'

    client = github.GitHubOAuth(
        "cid", "csecret", "https://dayfile.app/auth/github/callback", transport
    )
    assert client.identify("the-code", "the-verifier") == (7, "ann")
    form = parse_qs(sent[0][3].decode())
    assert form["code_verifier"] == ["the-verifier"] and form["client_secret"] == ["csecret"]
    assert sent[1][2]["Authorization"] == "Bearer gho_x" and sent[1][1] == github.USER_URL
    url = client.authorize_url("st", "ver")
    assert "scope" not in url and "code_challenge_method=S256" in url and "client_secret" not in url


@pytest.mark.parametrize(
    "token_reply,user_reply",
    [
        ((200, b"not json"), None),
        ((200, b'{"error": "bad_verification_code"}'), None),
        ((500, b""), None),
        ((200, b'{"access_token": "t"}'), (200, b"{}")),
        ((200, b'{"access_token": "t"}'), (200, b'{"id": "7", "login": "x"}')),
        ((200, b'{"access_token": "t"}'), (401, b"{}")),
    ],
)
def test_the_client_refuses_anything_odd_without_leaking_secrets(token_reply, user_reply):
    def transport(method, url, headers, body):
        return token_reply if url == github.TOKEN_URL else user_reply

    client = github.GitHubOAuth("cid", "csecret", "https://x/cb", transport)
    with pytest.raises(github.GitHubError) as e:
        client.identify("c", "v")
    assert "csecret" not in str(e.value) and "gho" not in str(e.value)


def test_the_real_transport_is_limited_to_fixed_github_hosts():
    assert github.AUTHORIZE_URL.startswith("https://github.com/")
    assert github.TOKEN_URL.startswith("https://github.com/")
    assert github.USER_URL.startswith("https://api.github.com/")
    assert re.fullmatch(r"[A-Za-z0-9_-]+", github.challenge("v" * 50))


def test_cookie_names_use_the_host_prefix_and_secure_flag_in_production(db, gh):
    app = create_web_app(accounts_db=db, github=gh, base_domain="example.test", site_origin=ORIGIN)
    r = TestClient(app, base_url=ORIGIN, follow_redirects=False).get("/auth/github")
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-dayfile-oauth=") and "Secure" in cookie and "Path=/" in cookie
    assert "Domain" not in cookie


def test_form_posts_with_a_null_origin_are_accepted_only_when_the_browser_says_same_origin(web, db):
    sign_in(web)
    form = {"invite": "AAAA-AAAA-AAAA", "terms": "yes"}
    same = {"Origin": "null", "Sec-Fetch-Site": "same-origin"}
    assert web.post("/signup", data=form, headers=same).status_code == 400  # past the check
    for headers in (
        {"Origin": "null", "Sec-Fetch-Site": "cross-site"},
        {"Origin": "null", "Sec-Fetch-Site": "same-site"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {},
    ):
        assert web.post("/signup", data=form, headers=headers).status_code == 403, headers


def test_open_signup_needs_only_the_terms(db, gh):
    app = create_web_app(
        accounts_db=db,
        github=gh,
        secure_cookies=False,
        clock=lambda: NOW[0],
        base_domain="example.test",
        site_origin=ORIGIN,
        fetch=lambda *a: None,
        signup="open",
    )
    web = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    sign_in(web)
    form = web.get("/signup").text
    assert 'name="invite"' not in form and "Beta" in form
    r = web.post("/signup", data={"terms": "yes"}, headers={"Origin": ORIGIN})
    assert r.status_code == 303 and r.headers["location"] == "/account"
    assert store.get_account(store.connect(db), github_id=101) is not None
    assert web.post("/signup", data={}, headers={"Origin": ORIGIN}).status_code in (303, 400)


def test_the_home_page_says_beta_and_no_longer_asks_for_a_code(web):
    page = web.get("/").text
    assert "Beta" in page and "Have a code" not in page
