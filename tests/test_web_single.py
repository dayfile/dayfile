"""The web app in single-log mode: one self-hosted log, a read-token sign-in, no hosted pages."""

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.auth.tokens import create_token
from dayfile.db import connect
from dayfile.web import create_web_app
from dayfile.web.client import ApiResult
from dayfile.web.creds import parse_single_token

COOKIE = "dayfile"  # no __Host- prefix when cookies are not secure


class Server:
    """A real single-log API and the web app in front of it."""

    def __init__(self, tmp_path):
        self.api = TestClient(create_app(tmp_path / "log.db", read_limit=(10000, 60)))
        conn = connect(tmp_path / "log.db")
        try:
            self.read = create_token(conn, ["self"]).value
            self.write = create_token(conn, ["self"], write_sources=["t"]).value
        finally:
            conn.close()
        self.api.post(
            "/events",
            json={"title": "Nightly backup finished", "tags": ["backup"]},
            headers={"Authorization": f"Bearer {self.write}"},
        )

    def fetch(self, slug, token, params):
        r = self.api.get(
            "/events", params=list(params), headers={"Authorization": f"Bearer {token}"}
        )
        ok = r.headers.get("content-type", "").startswith("application/json")
        return ApiResult(r.status_code, r.json() if ok else None)

    def web(self):
        app = create_web_app(fetch=self.fetch, single_log=True, secure_cookies=False)
        return TestClient(app, follow_redirects=False, base_url="http://box.lan:8001")


@pytest.fixture
def srv(tmp_path):
    return Server(tmp_path)


def sign_in(srv, web, token=None):
    return web.post(
        "/login", data={"token": token or srv.read}, headers={"Origin": "http://box.lan:8001"}
    )


def test_token_or_any_feed_url_names_the_log(srv):
    t = srv.read
    assert parse_single_token(t) == ("log", t)
    assert parse_single_token(f" {t} ") == ("log", t)
    for url in (
        f"https://feeds.example.com/t/{t}/feed.json",
        f"http://192.168.1.5:8000/t/{t}/feed.atom",
        f"https://x.example/t/{t}/f/todo-open.json",
    ):
        assert parse_single_token(url) == ("log", t)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "nope",
        "ftp://h/t/{t}/feed.json",
        "https://u:p@h/t/{t}/feed.json",
        "https://h/{t}/feed.json",
    ],
)
def test_junk_is_not_a_token(srv, text):
    assert parse_single_token(text.replace("{t}", srv.read)) is None


def test_the_bare_address_goes_to_sign_in_then_to_the_log(srv):
    web = srv.web()
    r = web.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = sign_in(srv, web)
    assert r.status_code == 303 and r.headers["location"] == "/log"
    assert web.get("/").headers["location"] == "/log"


def test_sign_in_asks_only_for_a_token(srv):
    page = srv.web().get("/login").text
    assert 'name="token"' in page and 'name="slug"' not in page and "username" not in page.lower()


def test_the_log_shows_events_and_offers_sign_out_not_posting(srv):
    web = srv.web()
    sign_in(srv, web)
    page = web.get("/log").text
    assert "Nightly backup finished" in page and "Sign out" in page
    assert 'href="/log/post"' not in page and "/account" not in page
    assert web.get("/log/new?after=0&released=2026-01-01T00:00:00Z").status_code == 200


def test_a_wrong_or_write_only_token_is_refused(srv):
    web = srv.web()
    r = sign_in(srv, web, "0123456789ab.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert r.status_code == 401 and "match" in r.text and COOKIE not in r.cookies
    r = sign_in(srv, web, srv.write)
    assert r.status_code == 403 and COOKIE not in r.cookies
    assert web.get("/log").status_code == 303  # still signed out


def test_a_post_from_another_site_is_refused(srv):
    r = srv.web().post("/login", data={"token": srv.read}, headers={"Origin": "https://evil.test"})
    assert r.status_code == 403


@pytest.mark.parametrize("path", ["/demo", "/pricing", "/terms", "/privacy", "/docs"])
def test_hosted_service_pages_are_not_there(srv, path):
    assert srv.web().get(path).status_code == 404


def test_the_footer_is_not_the_hosted_one(srv):
    page = srv.web().get("/login").text
    assert "Pricing" not in page and "Terms" not in page and "mailto:" not in page


def test_hosted_mode_is_unchanged(srv):
    web = TestClient(create_web_app(fetch=srv.fetch, single_log=False), follow_redirects=False)
    assert web.get("/pricing").status_code == 200
    assert 'name="slug"' in web.get("/login").text
