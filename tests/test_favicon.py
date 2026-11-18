"""The favicon is public static data: the same on every host, never tied to a credential."""

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.db import connect
from dayfile.tenancy import DirectoryTenancy


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "d.db"
    connect(path).close()
    return TestClient(create_app(path))


def test_svg_favicon_is_public_and_cacheable(client):
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "public" in r.headers["cache-control"] and "max-age" in r.headers["cache-control"]
    assert r.text.lstrip().startswith("<svg")


def test_ico_favicon_is_a_real_icon_file(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] in ("image/x-icon", "image/vnd.microsoft.icon")
    assert r.content[:4] == b"\x00\x00\x01\x00"  # ICO header: reserved 0, type 1


def test_favicon_sets_no_vary_on_authorization(client):
    # Not credential-dependent, so it must not be keyed (or hidden) by Authorization.
    assert "authorization" not in client.get("/favicon.svg").headers.get("vary", "").lower()


def test_favicon_needs_no_token_and_ignores_a_bad_one(client):
    r = client.get("/favicon.svg", headers={"Authorization": "Bearer nonsense"})
    assert r.status_code == 200


def test_favicon_is_served_on_a_tenant_host_even_when_suspended(tmp_path):
    tenancy = DirectoryTenancy(tmp_path / "t", "example.test")
    tenancy.create("acme", suspended=True)
    c = TestClient(create_app(tenancy=tenancy), base_url="https://acme.example.test")
    assert c.get("/favicon.svg").status_code == 200
    assert c.get("/events").status_code == 403  # the data stays shut


def test_head_requests_work(client):
    assert client.head("/favicon.ico").status_code == 200


@pytest.mark.parametrize("name,size", [("icon-512.png", 512), ("icon-64.png", 64)])
def test_png_icons_are_real_pngs_of_the_stated_size(client, name, size):
    r = client.get(f"/{name}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = (int.from_bytes(r.content[i : i + 4], "big") for i in (16, 20))
    assert width == height == size


def test_feeds_advertise_icons_on_their_own_host_without_the_token():
    from xml.etree import ElementTree as ET

    from dayfile.render.atom import render_atom
    from dayfile.render.jsonfeed import render_json

    url = "https://me.dayfile.cloud/t/abc123def456.SECRETSECRETSECRETSECRET/feed.json?tag=x"
    doc = render_json([], url)
    assert doc["icon"] == "https://me.dayfile.cloud/icon-512.png"
    assert doc["favicon"] == "https://me.dayfile.cloud/icon-64.png"
    icon = ET.fromstring(render_atom([], url, None)).find("{http://www.w3.org/2005/Atom}icon")
    assert icon is not None and icon.text == "https://me.dayfile.cloud/icon-512.png"
    assert "SECRET" not in doc["icon"] + doc["favicon"] + icon.text
