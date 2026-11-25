"""The account dashboard (M16d): naming a log, tokens and feeds, through the real admin API."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.tenancy import DirectoryTenancy
from dayfile.web.accounts import store
from dayfile.web.accounts.control import Control
from dayfile.web.app import create_web_app
from dayfile.web.client import ApiResult
from test_accounts_web import ORIGIN, StubGitHub, invite

SECRET = "admin-secret-" + "z" * 30
NOW = "2026-11-10T09:00:00Z"
POST = {"Origin": ORIGIN}


@pytest.fixture
def tenancy(tmp_path):
    return DirectoryTenancy(tmp_path / "tenants", "example.test")


@pytest.fixture
def core(tenancy, tmp_path):
    (tmp_path / "admin_secret").write_text(SECRET)
    app = create_app(
        tenancy=tenancy, admin_secret_file=tmp_path / "admin_secret", clock=lambda: NOW
    )
    return TestClient(app)


def host(core, slug):
    return TestClient(core.app, base_url=f"http://{slug}.example.test")


@pytest.fixture
def control(core):
    def send(method, path, body):
        r = core.request(method, path, json=body, headers={"Authorization": f"Bearer {SECRET}"})
        if r.headers.get("content-type") == "application/gzip":
            return ApiResult(r.status_code, r.content)
        return ApiResult(r.status_code, r.json() if r.content else None)

    return Control(send)


@pytest.fixture
def gh():
    g = StubGitHub()
    g.people["code-cy"] = (303, "cy")
    return g


@pytest.fixture
def db(tmp_path):
    return tmp_path / "accounts.db"


def make_web(db, gh, control, post=None, discovery=False):
    app = create_web_app(
        discovery=discovery,
        accounts_db=db,
        github=gh,
        control=control,
        secure_cookies=False,
        clock=lambda: NOW,
        base_domain="example.test",
        site_origin=ORIGIN,
        fetch=lambda *a: ApiResult(200, {"events": [], "cursor": 0, "released_cursor": NOW}),
        post=post,
    )
    return TestClient(app, base_url=ORIGIN, follow_redirects=False)


@pytest.fixture
def web(db, gh, control):
    return make_web(db, gh, control)


def sign_up(web, db, code="code-ann"):
    from urllib.parse import parse_qs, urlsplit

    r = web.get("/auth/github")
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    web.get("/auth/github/callback", params={"code": code, "state": state})
    r = web.post("/signup", data={"invite": invite(db), "terms": "yes"}, headers=POST)
    assert r.status_code == 303, r.text


def with_log(web, db, slug="acme", code="code-ann"):
    sign_up(web, db, code)
    r = web.post("/account/slug", data={"slug": slug}, headers=POST)
    assert r.status_code == 303, r.text


def token_from(page_text):
    return re.search(r"<code>([0-9a-f]{12}\.[A-Za-z0-9_-]+)</code>", page_text).group(1)


# --- the sign-up question (launch discovery, hosted only) ---


def answer_of(db):
    conn = store.connect(db)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM signup_answer")]
    finally:
        conn.close()


def test_the_question_is_off_by_default_and_ignored_when_off(web, db):
    sign_up(web, db)
    assert "What are you planning to log" not in web.get("/account").text
    r = web.post("/account/slug", data={"slug": "acme", "plans": "sneaky"}, headers=POST)
    assert r.status_code == 303 and answer_of(db) == []
    web.post("/account/answer", data={"plans": "sneaky"}, headers=POST)
    assert answer_of(db) == []
    assert "Contact and feedback" not in web.get("/account/settings").text


def test_skipping_the_question_changes_nothing(db, gh, control, tenancy):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    assert (
        'What are you planning to log? <span class="muted">(optional)' in web.get("/account").text
    )
    r = web.post("/account/slug", data={"slug": "acme"}, headers=POST)
    assert r.status_code == 303 and tenancy.get("acme") is not None and answer_of(db) == []


def test_an_answer_is_stored_as_plain_text_with_its_time(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    plans = "  Backups\r\nand \x1b[31mdeploys\u202e  "
    web.post("/account/slug", data={"slug": "acme", "plans": plans}, headers=POST)
    (row,) = answer_of(db)
    assert row["text"] == "Backups\nand [31mdeploys" and row["answered_at"] == NOW


def test_a_long_answer_is_refused_before_the_name_is_taken(db, gh, control, tenancy):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    r = web.post("/account/slug", data={"slug": "acme", "plans": "x" * 501}, headers=POST)
    assert r.status_code == 400 and "under 500" in r.text and "x" * 501 in r.text
    assert tenancy.get("acme") is None and answer_of(db) == []


def test_settings_shows_changes_and_removes_the_answer(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    web.post("/account/slug", data={"slug": "acme", "plans": "<b>bills</b>"}, headers=POST)
    page = web.get("/account/settings").text
    assert "&lt;b&gt;bills&lt;/b&gt;" in page and "<b>bills" not in page
    web.post("/account/answer", data={"plans": "trips"}, headers=POST)
    assert answer_of(db)[0]["text"] == "trips"
    assert web.post("/account/answer", data={"plans": "trips"}).status_code == 403
    web.post("/account/answer", data={"plans": "   "}, headers=POST)
    assert answer_of(db) == []


def account_json(web):
    """The account details as shown on the Settings page."""
    import html as htmllib

    page = web.get("/account/settings").text
    shown = re.search(r'<pre id="account-json">(.*?)</pre>', page, re.S).group(1)
    return json.loads(htmllib.unescape(shown))


def test_the_account_export_includes_the_answer(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    web.post("/account/slug", data={"slug": "acme", "plans": "bills"}, headers=POST)
    page = web.get("/account/settings").text
    assert "Show my account details" in page and "Download my account details" not in page
    got = account_json(web)
    assert got["account"]["slug"] == "acme" and got["account"]["github_login"] == "ann"
    assert got["signup_answer"] == {"text": "bills", "answered_at": NOW}
    assert web.post("/account/export-account", headers=POST).status_code in (404, 405)


def test_deleting_the_account_deletes_the_answer(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    web.post("/account/slug", data={"slug": "acme", "plans": "bills"}, headers=POST)
    r = web.post("/account/delete", data={"confirm": "acme"}, headers=POST)
    assert r.status_code == 200 and answer_of(db) == []


def email_of(db):
    conn = store.connect(db)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM contact_email")]
    finally:
        conn.close()


def test_the_email_field_is_off_by_default_and_ignored_when_off(web, db):
    sign_up(web, db)
    assert "Email <span" not in web.get("/account").text
    data = {"slug": "acme", "email": "ann@example.com", "consent": "yes"}
    assert web.post("/account/slug", data=data, headers=POST).status_code == 303
    web.post("/account/email", data={"email": "ann@example.com", "consent": "yes"}, headers=POST)
    assert email_of(db) == []


def test_the_consent_box_starts_unticked_with_the_exact_wording(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    page = web.get("/account").text
    assert '<input type="checkbox" name="consent" value="yes">' in page
    assert "The developer may email me about dayfile: service changes and one message" in page


def test_an_email_is_stored_only_with_consent_and_its_time(db, gh, control, tenancy):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    data = {"slug": "acme", "email": "Ann@Example.COM"}
    r = web.post("/account/slug", data=data, headers=POST)
    assert r.status_code == 400 and "Tick the box" in r.text and "Ann@Example.COM" in r.text
    assert tenancy.get("acme") is None and email_of(db) == []
    r = web.post("/account/slug", data={**data, "consent": "yes"}, headers=POST)
    assert r.status_code == 303
    (row,) = email_of(db)
    assert row["email"] == "Ann@example.com" and row["consented_at"] == NOW
    assert row["consent_version"] == store.EMAIL_CONSENT_VERSION


def test_ticking_the_box_with_no_email_stores_nothing(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    data = {"slug": "acme", "consent": "yes"}
    assert web.post("/account/slug", data=data, headers=POST).status_code == 303
    assert email_of(db) == []


@pytest.mark.parametrize("bad", ["ann", "ann@", "@example.com", "ann@localhost", "a b@x.io"])
def test_a_bad_email_is_refused_before_the_name_is_taken(db, gh, control, tenancy, bad):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    data = {"slug": "acme", "email": bad, "consent": "yes"}
    r = web.post("/account/slug", data=data, headers=POST)
    assert r.status_code == 400 and "email address" in r.text and tenancy.get("acme") is None


def test_settings_changes_and_removes_the_email(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    data = {"slug": "acme", "email": "ann@example.com", "consent": "yes"}
    web.post("/account/slug", data=data, headers=POST)
    assert "ann@example.com" in web.get("/account/settings").text
    web.post("/account/email", data={"email": "new@example.com"}, headers=POST)
    assert email_of(db)[0]["email"] == "ann@example.com"  # no consent ticked, no change
    web.post("/account/email", data={"email": "new@example.com", "consent": "yes"}, headers=POST)
    assert email_of(db)[0]["email"] == "new@example.com"
    assert web.post("/account/email", data={"remove": "yes"}).status_code == 403
    web.post("/account/email", data={"remove": "yes"}, headers=POST)
    assert email_of(db) == [] and "No email on file" in web.get("/account/settings").text


def test_the_export_includes_the_email_and_deleting_the_account_removes_it(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    data = {"slug": "acme", "email": "ann@example.com", "consent": "yes"}
    web.post("/account/slug", data=data, headers=POST)
    got = account_json(web)["contact_email"]
    assert got["email"] == "ann@example.com" and got["consented_at"] == NOW
    web.post("/account/delete", data={"confirm": "acme"}, headers=POST)
    assert email_of(db) == []


# --- naming the log ---


def test_a_new_account_is_asked_for_a_log_name_and_gets_a_real_tenant(web, db, tenancy):
    sign_up(web, db)
    assert 'name="slug"' in web.get("/account").text
    r = web.post("/account/slug", data={"slug": "Acme"}, headers=POST)
    assert r.status_code == 303
    assert tenancy.get("acme") is not None  # lower-cased, created through the admin API
    assert tenancy.get("acme").max_events == 50_000
    assert web.get("/account").headers["location"] == "/log"  # named: the account home is the log
    page = web.get("/account/settings").text
    assert "acme.example.test" in page and 'name="slug"' not in page
    assert store.get_account(store.connect(db), github_id=101)["slug"] == "acme"


@pytest.mark.parametrize(
    "bad", ["ab", "www", "admin", "dogfood", "a" * 40, "has space", "-x-", "UPPER_case"]
)
def test_bad_or_reserved_names_create_nothing(web, db, tenancy, bad):
    sign_up(web, db)
    assert web.post("/account/slug", data={"slug": bad}, headers=POST).status_code == 400
    assert tenancy.list() == []


def test_a_taken_name_is_refused_and_a_log_name_cannot_be_changed(web, db, tenancy, gh, control):
    with_log(web, db, "acme")
    other = make_web(db, gh, control)
    sign_up(other, db, "code-bob")
    assert other.post("/account/slug", data={"slug": "acme"}, headers=POST).status_code == 400
    assert web.post("/account/slug", data={"slug": "another"}, headers=POST).status_code == 400
    assert tenancy.list() == ["acme"]


def test_a_failed_tenant_creation_gives_the_name_back(db, gh, tenancy):
    broken = Control(lambda *a: ApiResult(500))
    web = make_web(db, gh, broken)
    sign_up(web, db)
    assert web.post("/account/slug", data={"slug": "acme"}, headers=POST).status_code == 503
    assert store.get_account(store.connect(db), github_id=101)["slug"] is None
    assert "acme" in web.get("/account").text or 'name="slug"' in web.get("/account").text


def test_without_a_control_connection_naming_is_unavailable(db, gh):
    web = make_web(db, gh, None)
    sign_up(web, db)
    assert web.post("/account/slug", data={"slug": "acme"}, headers=POST).status_code == 503


# --- tokens ---


def test_a_read_token_is_shown_once_and_works(web, db, core):
    with_log(web, db)
    r = web.post("/account/tokens", data={"kind": "read", "note": "reader"}, headers=POST)
    assert r.status_code == 200 and "only time it is shown" in r.text
    token = token_from(r.text)
    assert (
        host(core, "acme").get("/events", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )
    listing = web.get("/account/tokens").text
    assert token not in listing and token.split(".")[1] not in listing
    assert token.split(".")[0] in listing and "reader" in listing


def test_a_write_token_defaults_to_the_log_name_as_its_source(web, db, core):
    with_log(web, db)
    token = token_from(web.post("/account/tokens", data={"kind": "write"}, headers=POST).text)
    r = host(core, "acme").post(
        "/events", json={"title": "from my phone"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 201 and r.json()["source"] == {"id": "acme"}


def test_revoking_takes_two_steps_and_stops_the_token(web, db, core):
    with_log(web, db)
    token = token_from(web.post("/account/tokens", data={"kind": "read"}, headers=POST).text)
    token_id = token.split(".")[0]
    assert "Revoke token" in web.get(f"/account/tokens/{token_id}/revoke").text
    assert web.post(f"/account/tokens/{token_id}/revoke", headers=POST).status_code == 303
    assert (
        host(core, "acme").get("/events", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )
    assert token_id not in web.get("/account/tokens").text
    assert web.get("/account/tokens/not-an-id/revoke").status_code == 404


def test_one_account_cannot_see_or_revoke_anothers_tokens(web, db, core, gh, control):
    with_log(web, db, "acme")
    token = token_from(web.post("/account/tokens", data={"kind": "read"}, headers=POST).text)
    other = make_web(db, gh, control)
    with_log(other, db, "globex", "code-bob")
    assert token.split(".")[0] not in other.get("/account/tokens").text
    other.post(f"/account/tokens/{token.split('.')[0]}/revoke", headers=POST)
    assert (
        host(core, "acme").get("/events", headers={"Authorization": f"Bearer {token}"}).status_code
        == 200
    )


def test_the_token_limit_and_bad_input_are_reported_not_crashed(web, db):
    with_log(web, db)
    for _ in range(20):
        web.post("/account/tokens", data={"kind": "read"}, headers=POST)
    r = web.post("/account/tokens", data={"kind": "read"}, headers=POST)
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert "live tokens" in web.get(r.headers["location"]).text
    bad = web.post("/account/tokens", data={"kind": "admin"}, headers=POST)
    assert bad.status_code == 303 and "error=" in bad.headers["location"]


def test_viewer_tokens_are_hidden_and_the_viewer_opens(web, db):
    with_log(web, db)
    r = web.post("/account/view", headers=POST)
    assert r.status_code == 303 and r.headers["location"] == "/log?fresh=1"
    assert re.search(r"dayfile=acme:[0-9a-f]{12}\.", r.headers["set-cookie"])
    assert "dashboard viewer" not in web.get("/account/tokens").text


def test_the_log_tab_opens_the_viewer_for_a_signed_in_account(web, db):
    with_log(web, db)
    r = web.get("/log")
    assert r.status_code == 303 and r.headers["location"] == "/log?fresh=1"
    assert re.search(r"dayfile=acme:[0-9a-f]{12}\.", r.headers["set-cookie"])
    assert web.get("/log?fresh=1").status_code == 200


def test_sign_in_page_sends_a_signed_in_account_to_the_log(web, db):
    with_log(web, db)
    r = web.get("/login")
    assert r.status_code == 303 and r.headers["location"] == "/log"


def test_sign_in_page_shows_the_expired_notice_even_to_a_signed_in_account(web, db):
    with_log(web, db)  # no redirect here, or a failing log would loop back to it
    r = web.get("/login?expired=1")
    assert r.status_code == 200 and "session ended" in r.text


def test_polling_with_a_lapsed_viewer_token_goes_back_to_the_log_for_a_signed_in_account(web, db):
    with_log(web, db)
    r = web.get("/log/new?after=0&released=2026-11-10T09%3A00%3A00Z")
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/log"  # the log mints a new viewer


# --- feeds ---


def test_a_public_feed_made_in_the_dashboard_serves_strangers_until_made_private(web, db, core):
    with_log(web, db)
    form = {"name": "Blog", "title": "My blog", "tags": "blog", "visibility": "public"}
    assert web.post("/account/feeds", data=form, headers=POST).status_code == 303
    stranger = host(core, "acme")
    assert stranger.get("/f/blog.json").status_code == 200
    assert "My blog" in web.get("/account/feeds").text
    assert (
        web.post("/account/feeds", data={**form, "visibility": "private"}, headers=POST).status_code
        == 303
    )
    assert stranger.get("/f/blog.json").status_code == 401


def test_the_default_feed_can_be_published_without_a_filter(web, db, core):
    with_log(web, db)
    assert (
        web.post(
            "/account/feeds", data={"name": "default", "visibility": "public"}, headers=POST
        ).status_code
        == 303
    )
    assert host(core, "acme").get("/feed.json").status_code == 200


def test_feed_mistakes_are_reported_and_feeds_can_be_deleted(web, db, core):
    with_log(web, db)
    bad = web.post("/account/feeds", data={"name": "Bad_Name"}, headers=POST)
    assert bad.status_code == 303 and "error=" in bad.headers["location"]
    web.post("/account/feeds", data={"name": "work", "tags": "work"}, headers=POST)
    assert web.post("/account/feeds/work/delete", headers=POST).status_code == 303
    assert "/f/work.json" not in web.get("/account/feeds").text


# --- access control ---


def test_every_dashboard_page_needs_a_full_session(web, db):
    for path in (
        "/account",
        "/account/settings",
        "/account/tokens",
        "/account/feeds",
        "/account/tokens/abcdef012345/revoke",
    ):
        assert web.get(path).headers["location"] == "/login", path
    sign_up(
        TestClient(web.app, base_url=ORIGIN, follow_redirects=False), db, "code-cy"
    )  # unrelated
    pending = web  # a pending GitHub identity has no account
    r = pending.get("/auth/github")
    from urllib.parse import parse_qs, urlsplit

    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    pending.get("/auth/github/callback", params={"code": "code-ann", "state": state})
    assert pending.get("/account/tokens").headers["location"] == "/signup"


def test_every_dashboard_post_needs_the_same_origin(web, db):
    with_log(web, db)
    for path, data in [
        ("/account/slug", {"slug": "x" * 5}),
        ("/account/tokens", {"kind": "read"}),
        ("/account/tokens/abcdef012345/revoke", {}),
        ("/account/feeds", {"name": "w"}),
        ("/account/feeds/w/delete", {}),
        ("/account/view", {}),
    ]:
        assert web.post(path, data=data).status_code == 403, path
        assert (
            web.post(path, data=data, headers={"Origin": "https://evil.test"}).status_code == 403
        ), path


def test_the_admin_secret_and_token_secrets_never_reach_pages(web, db):
    with_log(web, db)
    new = web.post("/account/tokens", data={"kind": "write"}, headers=POST).text
    token = token_from(new)
    pages = "".join(
        web.get(p).text for p in ("/account/settings", "/account/tokens", "/account/feeds")
    )
    assert SECRET not in new + pages and token.split(".")[1] not in pages


def test_dashboard_pages_are_not_cached_and_carry_the_security_headers(web, db):
    with_log(web, db)
    for path in ("/account/settings", "/account/tokens", "/account/feeds"):
        r = web.get(path)
        assert r.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in r.headers["content-security-policy"]
        assert "style=" not in r.text
        assert not re.search(r"<script(?![^>]*\bsrc=)", r.text)  # no inline scripts


def test_token_changes_made_in_the_dashboard_appear_in_the_logs_own_feed(web, db, core, tenancy):
    from dayfile.db import connect

    with_log(web, db)
    web.post("/account/tokens", data={"kind": "read"}, headers=POST)
    web.post("/account/feeds", data={"name": "blog", "visibility": "public"}, headers=POST)
    rows = (
        connect(tenancy.get("acme").db_path)
        .execute("SELECT title FROM events ORDER BY seq")
        .fetchall()
    )
    assert [r["title"] for r in rows] == [
        "Token " + re.search(r"[0-9a-f]{12}", rows[0]["title"]).group(0) + " created (read)",
        "Feed blog published",
    ]


# --- renaming the username ---


def rename(web, new, ok="yes"):
    data = {"new_slug": new, **({"understood": ok} if ok else {})}
    return web.post("/account/rename", data=data, headers=POST)


def test_a_rename_moves_the_log_and_keeps_tokens_working_on_the_new_address(web, db, core, tenancy):
    with_log(web, db, "acme")
    token = token_from(web.post("/account/tokens", data={"kind": "read"}, headers=POST).text)
    assert rename(web, "Acme-New").status_code == 303
    assert tenancy.get("acme") is None and tenancy.get("acme-new") is not None
    assert store.get_account(store.connect(db), github_id=101)["slug"] == "acme-new"
    bearer = {"Authorization": f"Bearer {token}"}
    assert host(core, "acme-new").get("/events", headers=bearer).status_code == 200
    assert host(core, "acme").get("/events", headers=bearer).status_code == 404
    assert "acme-new.example.test" in web.get("/account/settings").text


def test_a_rename_is_recorded_in_the_logs_own_feed(web, db, tenancy):
    from dayfile.db import connect

    with_log(web, db, "acme")
    rename(web, "newname")
    rows = connect(tenancy.get("newname").db_path).execute("SELECT title FROM events").fetchall()
    assert [r["title"] for r in rows] == ["Username changed from acme to newname"]


def test_a_rename_cannot_take_anothers_name_or_a_reserved_one(web, db, gh, control, tenancy):
    with_log(web, db, "acme")
    other = make_web(db, gh, control)
    with_log(other, db, "globex", "code-bob")
    for bad in ("globex", "www", "ab", "dogfood", "acme"):
        assert rename(web, bad).status_code == 400, bad
    assert tenancy.list() == ["acme", "globex"]


def test_a_name_given_up_is_held_for_its_owner_for_a_day(web, db, gh, control, tenancy):
    with_log(web, db, "acme")
    rename(web, "acme2")
    other = make_web(db, gh, control)
    sign_up(other, db, "code-bob")
    assert other.post("/account/slug", data={"slug": "acme"}, headers=POST).status_code == 400
    assert tenancy.get("acme") is None  # not taken by anyone else
    assert rename(web, "acme").status_code == 303  # its owner can go back
    assert tenancy.get("acme") is not None


def test_a_held_name_is_released_after_a_day(web, db, gh, control, tenancy):
    import dayfile.web.accounts.store as s

    with_log(web, db, "acme")
    rename(web, "acme2")
    conn = store.connect(db)
    held = conn.execute("SELECT until FROM slug_hold").fetchone()["until"]
    assert held == "2026-11-11T09:00:00Z"  # one day after NOW
    conn.execute("UPDATE slug_hold SET until = '2026-11-10T08:00:00Z'")  # already past
    other = make_web(db, gh, control)
    sign_up(other, db, "code-bob")
    assert other.post("/account/slug", data={"slug": "acme"}, headers=POST).status_code == 303
    assert s.HOLD_SECONDS == 86400


def test_renaming_needs_confirmation_the_same_origin_and_is_limited(web, db):
    with_log(web, db, "acme")
    assert rename(web, "other", ok="").status_code == 400
    assert (
        web.post("/account/rename", data={"new_slug": "x" * 5, "understood": "yes"}).status_code
        == 403
    )
    for name in ("one-aaa", "two-aaa", "three-aaa"):
        assert rename(web, name).status_code == 303
    assert rename(web, "four-aaa").status_code == 400  # three a month


def test_a_failed_move_changes_nothing(db, gh, core):
    def send(method, path, body):
        if path.endswith("/rename"):
            return ApiResult(500)
        r = core.request(method, path, json=body, headers={"Authorization": f"Bearer {SECRET}"})
        return ApiResult(r.status_code, r.json() if r.content else None)

    web = make_web(db, gh, Control(send))
    with_log(web, db, "acme")
    assert rename(web, "newname").status_code == 503
    assert store.get_account(store.connect(db), github_id=101)["slug"] == "acme"
    assert web.post("/account/slug", data={"slug": "newname"}, headers=POST).status_code == 400


def test_the_admin_rename_refuses_bad_missing_and_taken_names(core, tenancy):
    auth = {"Authorization": f"Bearer {SECRET}"}
    base = "/internal/admin/tenants"
    core.put(f"{base}/acme", json={}, headers=auth)
    core.put(f"{base}/globex", json={}, headers=auth)

    def post(slug, to):
        return core.post(f"{base}/{slug}/rename", json={"to": to}, headers=auth)

    assert post("acme", "globex").status_code == 409
    assert post("acme", "www").status_code == 422
    assert post("acme", "Bad_Name").status_code == 422
    assert post("nope", "fresh").status_code == 404
    assert core.post(f"{base}/acme/rename", json={"to": "fresh"}).status_code == 401
    assert post("acme", "fresh").status_code == 200 and tenancy.list() == ["fresh", "globex"]


# --- posting from the log page ---


@pytest.fixture
def posting(db, gh, control, core):
    def post(slug, token, event):
        r = host(core, slug).post(
            "/events", json=event, headers={"Authorization": f"Bearer {token}"}
        )
        return ApiResult(r.status_code, r.json() if r.content else None)

    return make_web(db, gh, control, post=post)


def test_a_signed_in_account_can_post_from_the_log_page(posting, db, core):
    with_log(posting, db)
    form = posting.get("/log/post")
    assert form.status_code == 200 and 'name="tags"' in form.text and "todo" in form.text
    assert 'action="/log/post"' not in posting.get("/log?fresh=1").text
    r = posting.post("/log/post", data={"title": "Hello", "tags": "Work, launch"}, headers=POST)
    assert r.status_code == 303 and r.headers["location"] == "/log"
    assert re.search(r"dayfile-post=acme:[0-9a-f]{12}\.", r.headers["set-cookie"])
    got = host(core, "acme")
    token = token_from_cookie(posting)
    events = got.get("/events", headers={"Authorization": f"Bearer {read_token(posting)}"}).json()
    assert [e["title"] for e in events["events"] if e["title"] == "Hello"] == ["Hello"]
    assert events["events"][-1]["tags"] == ["work", "launch"] and token


def token_from_cookie(web):
    return web.cookies.get("dayfile-post", "").partition(":")[2]


def read_token(web):
    web.get("/log")  # opens the viewer, which sets the read cookie
    return web.cookies.get("dayfile", "").partition(":")[2]


def test_posting_reuses_its_write_token_and_remakes_it_when_it_lapses(posting, db):
    with_log(posting, db)
    posting.post("/log/post", data={"title": "one"}, headers=POST)
    first = token_from_cookie(posting)
    posting.post("/log/post", data={"title": "two"}, headers=POST)
    assert token_from_cookie(posting) == first
    jar = next(c for c in posting.cookies.jar if c.name == "dayfile-post")
    posting.cookies.set("dayfile-post", "acme:" + first[:12] + "." + "x" * 43, domain=jar.domain)
    r = posting.post("/log/post", data={"title": "three"}, headers=POST)
    assert r.headers["location"] == "/log" and token_from_cookie(posting) != first


def test_the_log_pages_write_token_is_hidden_and_limited_to_one_source(posting, db):
    with_log(posting, db)
    posting.post("/log/post", data={"title": "x"}, headers=POST)
    assert "dashboard viewer" not in posting.get("/account/tokens").text


def test_an_empty_title_and_a_cross_site_post_are_refused(posting, db):
    with_log(posting, db)
    r = posting.post("/log/post", data={"title": "  ", "tags": "keep"}, headers=POST)
    assert r.status_code == 400 and "Give it a title" in r.text and 'value="keep"' in r.text
    assert posting.post("/log/post", data={"title": "x"}).status_code == 403


def test_posting_needs_an_account_session(web):
    assert web.post("/log/post", data={"title": "x"}, headers=POST).headers["location"] == "/login"


def test_a_post_with_state_open_is_a_todo_and_one_without_has_no_status(posting, db, core):
    with_log(posting, db)
    posting.post("/log/post", data={"title": "x", "tags": "todo"}, headers=POST)
    posting.post("/log/post", data={"title": "plain"}, headers=POST)
    by_title = {e["title"]: e for e in events_of(core, posting)}
    assert by_title["x"]["status"] == "open" and by_title["x"]["tags"] == ["todo"]
    assert "status" not in by_title["plain"]


def test_the_log_page_opens_posting_in_a_dialog_and_has_no_post_tab(posting, db):
    with_log(posting, db)
    posting.get("/log")
    page = posting.get("/log?fresh=1").text
    assert 'id="post-open" href="/log/post"' in page and '<dialog id="post-dialog"' in page
    assert 'src="/static/log.js?v=' in page and "<script>" not in page
    assert ">Post</a>" not in page.split("</header>")[0]  # not a header tab
    assert posting.get("/static/log.js").status_code == 200


# --- closing events from the log ---


def events_of(core, posting, slug="acme"):
    token = read_token(posting)
    got = host(core, slug).get(
        "/events?status=open&status=closed&status=none&status=retracted&status=superseded",
        headers={"Authorization": f"Bearer {token}"},
    )
    return got.json()["events"]


def test_mark_done_and_retract_post_events_that_point_back_and_inherit_the_title(posting, db, core):
    with_log(posting, db)
    posting.post("/log/post", data={"title": "Dentist", "tags": "todo"}, headers=POST)
    (orig,) = [e for e in events_of(core, posting) if e["title"] == "Dentist"]
    r = posting.post(f"/log/event/{orig['id']}/close", headers=POST)
    assert r.status_code == 303 and r.headers["location"] == "/log"
    events = events_of(core, posting)
    closing = [e for e in events if e.get("refs")]
    assert [e["title"] for e in closing] == ["Dentist"]  # inherited from the event it closes
    assert closing[0]["refs"] == [{"rel": "completes", "id": orig["id"]}]
    assert [e["status"] for e in events if e["id"] == orig["id"]] == ["closed"]
    posting.post(f"/log/event/{orig['id']}/retract", headers=POST)
    after = {e["id"]: e for e in events_of(core, posting)}
    assert after[orig["id"]]["status"] == "retracted"


def test_closing_needs_a_real_id_action_and_origin(posting, db):
    with_log(posting, db)
    ok_id = "01JC8Z4Q7M2X9K3T5W6N8R0VBA"
    assert posting.post(f"/log/event/{ok_id}/explode", headers=POST).status_code == 404
    assert posting.post("/log/event/not-an-id/close", headers=POST).status_code == 404
    assert posting.post(f"/log/event/{ok_id}/close").status_code == 403


def test_the_log_shows_close_buttons_only_for_signed_in_accounts_and_open_events(db, gh, control):
    shown = {"status": "open"}

    def fetch(*args):
        event = {
            "id": "01JC8Z4Q7M2X9K3T5W6N8R0VBA",
            "title": "Dentist",
            "status": shown["status"],
            "source": {"id": "web"},
            "seq": 1,
            "visible_at": NOW,
            "tags": [],
        }
        return ApiResult(200, {"events": [event], "cursor": 1, "released_cursor": NOW})

    app = create_web_app(
        accounts_db=db,
        github=gh,
        control=control,
        secure_cookies=False,
        clock=lambda: NOW,
        base_domain="example.test",
        site_origin=ORIGIN,
        fetch=fetch,
    )
    web = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    with_log(web, db)
    web.get("/log")
    open_page = web.get("/log?fresh=1").text
    assert ">Close</button>" in open_page and "Retract this event" in open_page
    shown["status"] = "closed"
    done_page = web.get("/log?fresh=1").text
    assert ">Close</button>" not in done_page and "Retract this event" in done_page
    shown["status"] = "retracted"
    assert "Retract this event" not in web.get("/log?fresh=1").text
    web.cookies.clear()
    web.cookies.set("dayfile", "acme:0123456789ab." + "A" * 43)
    shown["status"] = "open"
    read_only = web.get("/log").text
    assert ">Close</button>" not in read_only and "Retract this event" not in read_only


def test_the_log_hides_events_that_only_close_another_and_the_original_shows_its_state(
    db, gh, control
):
    def event(n, **extra):
        return {
            "id": f"01JC8Z4Q7M2X9K3T5W6N8R0VB{n}",
            "title": "Dentist",
            "source": {"id": "web"},
            "seq": n,
            "visible_at": NOW,
            "tags": [],
            **extra,
        }

    events = [
        event(1, status="closed"),
        event(2, refs=[{"rel": "completes", "id": "01JC8Z4Q7M2X9K3T5W6N8R0VB1"}]),
        event(3, title="Reply", refs=[{"rel": "replies-to", "id": "01JC8Z4Q7M2X9K3T5W6N8R0VB1"}]),
    ]
    body = {"events": events, "cursor": 3, "released_cursor": NOW}
    app = create_web_app(
        accounts_db=db,
        github=gh,
        control=control,
        secure_cookies=False,
        clock=lambda: NOW,
        base_domain="example.test",
        site_origin=ORIGIN,
        fetch=lambda *a: ApiResult(200, body),
    )
    web = TestClient(app, base_url=ORIGIN, follow_redirects=False)
    with_log(web, db)
    web.get("/log")
    page = web.get("/log?fresh=1").text
    assert page.count("<article") == 2 and page.count(">Dentist</h3>") == 1  # the closer is hidden
    assert ">Reply</h3>" in page  # a reply is a real entry
    assert (
        'class="ref"' in page and ">0vb1<" not in page
    )  # a lowercase short id, never the whole one
    assert ">source: web<" in page and "#1" not in page  # no bare log position on the card
    assert 'title="Source: who sent this event"' in page


# --- history of an event ---


@pytest.fixture
def live(db, gh, control, core):
    def call(method):
        def go(slug, token, arg):
            h = {"Authorization": f"Bearer {token}"}
            c = host(core, slug)
            r = (
                c.get("/events", params=arg, headers=h)
                if method == "get"
                else c.post("/events", json=arg, headers=h)
            )
            return ApiResult(r.status_code, r.json() if r.content else None)

        return go

    return make_web(db, gh, control, post=call("post")), call("get")


def test_close_with_a_reason_saves_it_and_history_shows_it(live, db, core):
    web, _ = live
    web.app.state.fetch = lambda slug, token, params: live[1](slug, token, params)
    with_log(web, db)
    web.post("/log/post", data={"title": "Dentist", "tags": "todo"}, headers=POST)
    web.get("/log")
    original = re.search(r"/log/event/([0-9A-Z]{26})/close", web.get("/log?fresh=1").text).group(1)
    r = web.post(f"/log/event/{original}/close", data={"note": "Booked for Friday."}, headers=POST)
    assert r.status_code == 303
    web.get("/log")
    page = web.get("/log?fresh=1").text
    assert "Booked for Friday." in page


def test_history_shows_the_chain_and_undoing_steps_changes_the_status(live, db, core):
    web, _ = live
    app_fetch = web.app.state
    app_fetch.fetch = lambda slug, token, params: live[1](slug, token, params)
    with_log(web, db)
    web.post("/log/post", data={"title": "Dentist", "tags": "todo"}, headers=POST)

    UNDO = (
        r"/log/event/([0-9A-Z]{26})/retract\"><button class=\"link\" "
        r"type=\"submit\" title=\"Undo this step"
    )

    def card():
        web.get("/log")
        return web.get("/log?fresh=1").text

    page = card()
    assert (
        "<summary>History</summary>" in page and ">Close</button>" in page and "Closed" not in page
    )
    original = re.search(r"/log/event/([0-9A-Z]{26})/close", page).group(1)
    web.post(f"/log/event/{original}/close", headers=POST)
    page = card()
    assert 'class="chip state"' in page and ">closed</span>" in page
    assert "<summary>History (1)" in page and "Closed" in page and "<em>undone</em>" not in page
    step = re.search(
        UNDO,
        page,
    ).group(1)
    web.post(f"/log/event/{step}/retract", headers=POST)  # undo the completion
    page = card()
    assert ">open</span>" in page and "<em>undone</em>" in page and ">Close</button>" in page
    undo = re.findall(
        UNDO,
        page,
    )
    assert undo and undo[-1] != step  # the retraction itself can be undone
    web.post(f"/log/event/{undo[-1]}/retract", headers=POST)  # undo the undo
    page = card()
    assert ">closed</span>" in page


def test_feed_names_link_to_the_public_feed_or_to_the_filtered_log(web, db):
    with_log(web, db)
    web.post("/account/feeds", data={"name": "blog", "visibility": "public"}, headers=POST)
    web.post("/account/feeds", data={"name": "work", "tags": "work"}, headers=POST)
    page = web.get("/account/feeds").text
    assert 'href="https://acme.example.test/f/blog.json"' in page
    assert 'href="/log?tag=work"' in page and 'href="/log"' in page  # the default feed too


# --- export and delete (M16e) ---


def test_export_downloads_a_gzipped_database_that_passes_its_integrity_check(web, db, tmp_path):
    import gzip
    import sqlite3

    with_log(web, db)
    web.post("/account/tokens", data={"kind": "read"}, headers=POST)
    r = web.post("/account/export", headers=POST)
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    assert 'filename="acme-dayfile.db.gz"' in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    copy = tmp_path / "export.db"
    copy.write_bytes(gzip.decompress(r.content))
    conn = sqlite3.connect(copy)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] >= 1
    assert web.post("/account/export").status_code == 403  # needs the same origin


def test_deleting_needs_the_username_and_then_removes_the_log_the_sessions_and_the_name(
    web, db, tenancy, core
):
    with_log(web, db)
    token = token_from(web.post("/account/tokens", data={"kind": "read"}, headers=POST).text)
    wrong = web.post("/account/delete", data={"confirm": "nope"}, headers=POST)
    assert wrong.status_code == 400 and tenancy.get("acme") is not None
    r = web.post("/account/delete", data={"confirm": "Acme"}, headers=POST)
    assert r.status_code == 200 and "Your account is deleted" in r.text
    assert tenancy.get("acme") is None and not (tenancy.root / "acme").exists()
    assert not list((tenancy.root / ".trash").glob("acme*"))  # erased, not trashed
    bearer = {"Authorization": f"Bearer {token}"}
    assert host(core, "acme").get("/events", headers=bearer).status_code == 404
    conn = store.connect(db)
    assert store.get_account(conn, github_id=101) is None  # the GitHub identity is free again
    (row,) = [a for a in store.list_accounts(conn) if a["status"] == "deleted"]
    assert row["slug"] is None and row["deleted_at"] and row["github_id"] < 0
    assert conn.execute("SELECT count(*) FROM session").fetchone()[0] == 0
    hold = conn.execute("SELECT slug FROM slug_hold").fetchall()
    assert [h["slug"] for h in hold] == ["acme"]
    assert web.get("/account").headers["location"] == "/login"  # the session is gone
    audit = [a["action"] for a in conn.execute("SELECT action FROM audit")]
    assert "account.delete" in audit


def test_a_deleted_account_cannot_sign_in_again(web, db, gh):
    with_log(web, db)
    web.post("/account/delete", data={"confirm": "acme"}, headers=POST)
    other = make_web(db, gh, None)
    from urllib.parse import parse_qs, urlsplit

    r = other.get("/auth/github")
    state = parse_qs(urlsplit(r.headers["location"]).query)["state"][0]
    back = other.get("/auth/github/callback", params={"code": "code-ann", "state": state})
    assert back.status_code in (200, 400, 403) and "Your Log" not in back.text
    assert other.get("/account").headers["location"] in ("/login", "/signup")


def test_a_failed_tenant_delete_leaves_the_account_untouched(web, db, tenancy, control):
    with_log(web, db)
    real = control.delete_tenant
    control.delete_tenant = lambda slug: ApiResult(503)
    r = web.post("/account/delete", data={"confirm": "acme"}, headers=POST)
    control.delete_tenant = real
    assert r.status_code == 503 and tenancy.get("acme") is not None
    assert store.get_account(store.connect(db), github_id=101)["status"] == "active"


def test_deleting_works_again_after_the_log_was_already_erased(web, db, tenancy):
    with_log(web, db)
    tenancy.erase("acme")  # an earlier attempt got this far and then failed
    r = web.post("/account/delete", data={"confirm": "acme"}, headers=POST)
    assert r.status_code == 200 and "Your account is deleted" in r.text


def test_an_empty_log_shows_how_to_send_the_first_event(posting, db, core):
    with_log(posting, db)
    posting.get("/log")
    page = posting.get("/log?fresh=1").text
    assert "Send the first event" in page and 'action="/account/tokens"' in page
    assert "?tag=" not in page  # no filters applied, so this is the true empty state


def test_an_empty_log_offers_a_write_token_you_already_have(posting, db, core):
    with_log(posting, db)
    made = posting.post("/account/tokens", data={"kind": "write"}, headers=POST).text
    secret = token_from(made)
    posting.get("/log")
    page = posting.get("/log?fresh=1").text
    assert "Send the first event" in page and secret.split(".")[0] in page
    assert "YOUR_TOKEN" in page and secret not in page  # the id, never the secret


def test_the_token_form_says_token_type_and_hides_the_source_until_write(web, db):
    with_log(web, db)
    page = web.get("/account/tokens").text
    assert "Token type" in page and ">Kind<" not in page
    assert '<div id="source-field" hidden>' in page
    assert "/static/tokens.js?v=" in page
    assert web.get(re.search(r"/static/tokens\.js\?v=\w+", page).group(0)).status_code == 200


# --- a full log ---


def test_fullness_names_the_worst_of_events_and_storage():
    from dayfile.web.app import fullness

    base = {"events": 10, "max_events": 100, "bytes": 10, "max_bytes": 100}
    assert fullness(base) == ""
    assert fullness({**base, "bytes": 90}) == "near"
    assert fullness({**base, "events": 100}) == "full"
    assert fullness({"events": 5, "max_events": None}) == "" and fullness(None) == ""


def test_the_log_page_warns_when_the_log_is_nearly_or_completely_full(posting, db, tenancy):
    with_log(posting, db)
    assert "nearly full" not in posting.get("/log").text
    tenancy.update("acme", max_events=1)
    assert "This log is nearly full" not in posting.get("/log").text  # empty: 0 of 1
    posting.post("/log/post", data={"title": "one"}, headers=POST)
    page = posting.get("/log").text
    assert "This log is full" in page and "start a fresh log" in page


def test_a_refused_post_says_the_log_is_full_and_where_to_go(posting, db, tenancy):
    with_log(posting, db)
    tenancy.update("acme", max_events=1)
    posting.post("/log/post", data={"title": "one"}, headers=POST)
    r = posting.post("/log/post", data={"title": "two"}, headers=POST)
    assert "start a fresh log in Settings" in r.text


# --- starting a fresh log ---


def test_starting_fresh_needs_the_username_and_leaves_an_empty_log_under_the_same_name(
    web, db, tenancy
):
    with_log(web, db)
    web.post("/account/tokens", data={"kind": "read"}, headers=POST)
    web.post("/log/post", data={"title": "one"}, headers=POST)
    wrong = web.post("/account/reset", data={"confirm": "nope"}, headers=POST)
    assert wrong.status_code == 303 and "exactly" in wrong.headers["location"]
    assert tenancy.usage("acme")["events"] >= 1
    r = web.post("/account/reset", data={"confirm": "Acme"}, headers=POST)
    assert r.status_code == 303 and r.headers["location"] == "/log"
    assert tenancy.get("acme").max_events == 50_000
    assert tenancy.usage("acme")["events"] == 0
    assert not list((tenancy.root / ".trash").glob("acme*"))
    assert web.post("/account/reset", data={"confirm": "acme"}).status_code == 403  # same origin


# --- heartbeats ---


def test_heartbeats_are_made_and_removed_on_the_feeds_page(web, db, core):
    with_log(web, db)
    form = {"name": "Nightly-Backup", "watch": "source", "value": "backup-script", "every": "24h"}
    assert web.post("/account/heartbeats", data=form, headers=POST).status_code == 303
    page = web.get("/account/feeds/heartbeats").text
    assert "nightly-backup" in page and "backup-script" in page and "24h" in page
    assert (
        'aria-current="page">Heartbeats' in page
        and "nightly-backup" not in web.get("/account/feeds").text
    )
    bad = web.post("/account/heartbeats", data={**form, "every": "soon"}, headers=POST)
    assert bad.status_code == 303 and bad.headers["location"].startswith(
        "/account/feeds/heartbeats?error="
    )
    assert web.post("/account/heartbeats", data=form).status_code == 403  # same origin only
    web.post("/account/heartbeats/nightly-backup/delete", headers=POST)
    assert "No heartbeats yet" in web.get("/account/feeds/heartbeats").text


def test_a_deleted_account_can_sign_up_again_with_the_same_github_account(web, db):
    with_log(web, db)
    web.post("/account/delete", data={"confirm": "acme"}, headers=POST)
    sign_up(web, db, "code-ann")  # the same GitHub identity (id 101) as before
    assert web.post("/account/slug", data={"slug": "acme2"}, headers=POST).status_code == 303


def test_the_same_person_can_retake_the_name_they_just_deleted_but_others_cannot(web, db, gh):
    gh.people["code-dee"] = (202, "dee")
    with_log(web, db, "acme")
    web.post("/account/delete", data={"confirm": "acme"}, headers=POST)
    web.cookies.clear()
    sign_up(web, db, "code-dee")  # someone else
    taken = web.post("/account/slug", data={"slug": "acme"}, headers=POST)
    assert taken.status_code != 303 and "taken" in taken.text
    web.cookies.clear()
    sign_up(web, db, "code-ann")  # the original owner is back
    assert web.post("/account/slug", data={"slug": "acme"}, headers=POST).status_code == 303


def test_the_home_page_sends_a_signed_in_account_to_its_log_and_a_signed_out_one_nowhere(web, db):
    assert web.get("/", follow_redirects=False).status_code == 200
    with_log(web, db)
    r = web.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/log"
    assert r.headers["cache-control"] == "no-store"
    web.post("/account/logout", headers=POST)
    assert web.get("/", follow_redirects=False).status_code == 200


# --- roadmap interest (launch discovery, hosted only) ---


def interest_rows(db):
    conn = store.connect(db)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM feature_interest ORDER BY id")]
    finally:
        conn.close()


def ask(web, *features, **more):
    return web.post("/pricing/interest", data={"feature": list(features), **more}, headers=POST)


def test_the_roadmap_is_off_without_the_flag_and_has_one_button_with_it(db, gh, control):
    off = make_web(db, gh, control).get("/pricing").text
    assert "Roadmap" not in off and "Tell me when" not in off
    r = ask(make_web(db, gh, control), "teams")
    assert r.headers["location"] == "/pricing" and interest_rows(db) == []
    on = make_web(db, gh, control, discovery=True).get("/pricing").text
    assert on.count("not available yet") == 3 and on.count("Tell me when these exist") == 1
    assert on.count('type="checkbox" name="feature"') == 3 and "Team logs" in on


def test_a_signed_out_request_is_one_form_counts_each_choice_and_sets_no_cookie(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    r = ask(web, "alerts", "teams")
    assert (
        r.status_code == 303
        and "set-cookie" not in r.headers
        and "thanks=1" in r.headers["location"]
    )
    rows = interest_rows(db)
    assert [x["feature"] for x in rows] == ["alerts", "teams"]
    assert all(
        x["account_id"] is None and x["email"] is None and x["created_at"] == NOW for x in rows
    )
    assert "error=" in ask(web).headers["location"]  # nothing ticked


def test_a_signed_out_address_needs_the_consent_box_and_a_plausible_form(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    assert "error=" in ask(web, "teams", email="a@example.com").headers["location"]
    assert "error=" in ask(web, "teams", email="nope", consent="yes").headers["location"]
    assert interest_rows(db) == []
    good = {"email": "Bo@Example.COM", "consent": "yes"}
    ask(web, "teams", "alerts", **good)
    ask(web, "teams", **good)  # the same address for the same feature is one row
    rows = interest_rows(db)
    assert sorted(x["feature"] for x in rows) == ["alerts", "teams"]
    assert {x["email"] for x in rows} == {"Bo@example.com"}
    assert {x["consent_version"] for x in rows} == {store.EMAIL_CONSENT_VERSION}


def test_a_signed_in_save_replaces_the_set_and_shows_the_ticks(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    web.post("/account/slug", data={"slug": "acme"}, headers=POST)
    assert "saved=1" in ask(web, "sharing", "teams").headers["location"]
    assert sorted(x["feature"] for x in interest_rows(db)) == ["sharing", "teams"]
    page = web.get("/pricing").text
    assert "Save my choices" in page and page.count(" checked>") == 2
    ask(web, "teams", "alerts")
    assert sorted(x["feature"] for x in interest_rows(db)) == ["alerts", "teams"]
    ask(web)
    assert interest_rows(db) == []


def test_interest_is_exported_deleted_with_the_account_and_counted_without_names(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    web.post("/account/slug", data={"slug": "acme"}, headers=POST)
    ask(web, "teams")
    ask(make_web(db, gh, control, discovery=True), "teams")
    got = account_json(web)["feature_interest"]
    assert got == [{"feature": "teams", "created_at": NOW}]
    conn = store.connect(db)
    try:
        counts = store.account_stats(conn, NOW)["feature_interest"]
    finally:
        conn.close()
    assert counts["teams"] == {"signed_in": 1, "signed_out": 1}
    assert counts["alerts"] == {"signed_in": 0, "signed_out": 0}
    web.post("/account/delete", data={"confirm": "acme"}, headers=POST)
    assert [r["account_id"] for r in interest_rows(db)] == [None]  # the signed-out one stays


def test_unknown_features_and_cross_site_posts_are_refused(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    assert "error=" in ask(web, "bitcoin").headers["location"]
    assert web.post("/pricing/interest", data={"feature": "teams"}).status_code == 403
    assert interest_rows(db) == []


def test_the_interest_endpoint_is_rate_limited_in_memory(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    codes = [ask(web, "teams").status_code for _ in range(32)]
    assert codes[:30] == [303] * 30 and 429 in codes[30:]


def test_pricing_has_no_sign_in_or_open_your_log_for_someone_signed_in(db, gh, control):
    web = make_web(db, gh, control)
    assert "Sign in with GitHub" in web.get("/pricing").text
    sign_up(web, db)
    web.post("/account/slug", data={"slug": "acme"}, headers=POST)
    page = web.get("/pricing").text
    assert "Sign in with GitHub" not in page and "Open your log" not in page
    assert web.get("/pricing").headers["cache-control"] == "no-store"


def test_contact_and_feedback_save_together_with_one_button(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    web.post("/account/slug", data={"slug": "acme"}, headers=POST)
    page = web.get("/account/settings").text
    section = page.split("Contact and feedback")[1].split("danger-zone")[0]
    assert section.count(">Save<") == 1 and 'action="/account/contact"' in section
    both = {"email": "ann@example.com", "consent": "yes", "plans": "deploy notes"}
    assert web.post("/account/contact", data=both, headers=POST).status_code == 303
    assert (
        email_of(db)[0]["email"] == "ann@example.com" and answer_of(db)[0]["text"] == "deploy notes"
    )
    # saving the answer alone keeps the email and its consent time
    again = {"email": "ann@example.com", "plans": "backups"}
    web.post("/account/contact", data=again, headers=POST)
    assert email_of(db)[0]["consented_at"] == NOW and answer_of(db)[0]["text"] == "backups"
    # clearing both boxes removes both
    web.post("/account/contact", data={"email": "", "plans": ""}, headers=POST)
    assert email_of(db) == [] and answer_of(db) == []


def test_a_bad_contact_save_writes_nothing(db, gh, control):
    web = make_web(db, gh, control, discovery=True)
    sign_up(web, db)
    web.post("/account/slug", data={"slug": "acme"}, headers=POST)
    for data in (
        {"email": "ann@example.com", "plans": "x"},  # no consent for a new address
        {"email": "nope", "consent": "yes", "plans": "x"},
        {"email": "ann@example.com", "consent": "yes", "plans": "y" * 501},
    ):
        r = web.post("/account/contact", data=data, headers=POST)
        assert "error=" in r.headers["location"]
    assert email_of(db) == [] and answer_of(db) == []
    assert web.post("/account/contact", data={"plans": "x"}).status_code == 403


def test_event_id_badges_carry_the_full_id_and_the_copy_script_is_served(live, db):
    web, _ = live
    web.app.state.fetch = lambda slug, token, params: live[1](slug, token, params)
    with_log(web, db)
    web.post("/log/post", data={"title": "Dentist", "tags": "todo"}, headers=POST)
    web.get("/log")
    page = web.get("/log?fresh=1").text
    full = re.search(r'data-id="([0-9A-Z]{26})"', page).group(1)
    assert f">{full[-6:].lower()}</code>" in page and f"Event {full}" in page
    assert 'src="/static/copy-id.js?v=' in page and "<script>" not in page
    script = web.get("/static/copy-id.js")
    assert script.status_code == 200 and "dataset.id" in script.text


def test_polling_reloads_the_page_when_something_was_closed_and_clears_the_empty_message(live, db):
    web, _ = live
    web.app.state.fetch = lambda slug, token, params: live[1](slug, token, params)
    with_log(web, db)
    web.get("/log")
    empty = web.get("/log?fresh=1").text
    assert 'id="empty-state"' in empty
    web.post("/log/post", data={"title": "Dentist", "tags": "todo"}, headers=POST)
    web.get("/log")
    page = web.get("/log?fresh=1").text
    poll = re.search(r'hx-get="(/log/new\?[^"]+)"', page).group(1).replace("&amp;", "&")
    plain = web.get(poll)
    assert "HX-Refresh" not in plain.headers
    web.post("/log/post", data={"title": "Lunch"}, headers=POST)
    arrived = web.get(poll)
    assert "Lunch" in arrived.text and 'id="empty-state" hx-swap-oob="delete"' in arrived.text
    original = re.search(r"/log/event/([0-9A-Z]{26})/close", page).group(1)
    web.post(f"/log/event/{original}/close", headers=POST)
    assert web.get(poll).headers.get("HX-Refresh") == "true"
    assert web.get(poll + "&tag=todo").headers.get("HX-Refresh") == "true"  # filtered view too
