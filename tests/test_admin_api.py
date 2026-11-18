"""The admin API (M16b): the narrow control surface the web app uses to run tenants."""

import logging

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.auth.tokens import verify_token
from dayfile.db import connect
from dayfile.tenancy import DirectoryTenancy

SECRET = "s3cret-" + "a" * 40
BASE = "example.test"
AUTH = {"Authorization": f"Bearer {SECRET}"}


@pytest.fixture
def tenancy(tmp_path):
    return DirectoryTenancy(tmp_path / "tenants", BASE)


@pytest.fixture
def secret_file(tmp_path):
    path = tmp_path / "admin_secret"
    path.write_text(SECRET + "\n")
    return path


@pytest.fixture
def client(tenancy, secret_file):
    return TestClient(create_app(tenancy=tenancy, admin_secret_file=secret_file))


def put(client, slug="acme", **body):
    return client.put(f"/internal/admin/tenants/{slug}", json=body, headers=AUTH)


def mint(client, slug="acme", **body):
    return client.post(f"/internal/admin/tenants/{slug}/tokens", json=body, headers=AUTH)


def test_without_a_secret_file_the_routes_do_not_exist(tenancy, tmp_path):
    app = create_app(tenancy=tenancy, admin_secret_file=tmp_path / "missing")
    assert TestClient(app).put("/internal/admin/tenants/acme", headers=AUTH).status_code == 404


def test_every_route_needs_the_secret(client):
    put(client)
    for method, path in [
        ("put", "/internal/admin/tenants/acme"),
        ("get", "/internal/admin/tenants/acme"),
        ("post", "/internal/admin/tenants/acme/tokens"),
        ("get", "/internal/admin/tenants/acme/tokens"),
        ("delete", "/internal/admin/tenants/acme/tokens/abc"),
        ("post", "/internal/admin/tenants/acme/suspend"),
        ("delete", "/internal/admin/tenants/acme"),
    ]:
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": SECRET}):
            r = getattr(client, method)(path, headers=headers)
            assert r.status_code == 401, (method, path)


def test_create_is_idempotent_and_applies_limits(client, tenancy):
    r = put(client, max_events=50, write_limit=[60, 60])
    assert r.status_code == 201 and r.json()["created"] is True
    again = put(client, max_events=999)
    assert again.status_code == 200 and again.json()["created"] is False
    assert tenancy.get("acme").max_events == 50  # a repeat does not change the plan


def test_bad_names_and_limits_are_refused(client):
    assert put(client, slug="www").status_code == 422
    assert put(client, slug="Bad_Name").status_code == 422
    assert put(client, slug="ok", max_events=-1).status_code == 422
    assert put(client, slug="ok2", write_limit="fast").status_code == 422


def test_usage_reports_events_limits_and_state(client):
    put(client, max_events=10)
    body = client.get("/internal/admin/tenants/acme", headers=AUTH).json()
    assert body["events"] == 0 and body["max_events"] == 10 and body["suspended"] is False
    assert client.get("/internal/admin/tenants/nope", headers=AUTH).status_code == 404


def test_minted_tokens_work_and_the_secret_is_returned_once(client, tenancy):
    put(client)
    r = mint(client, kind="read", labels=["self"], note="phone")
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    value = r.json()["token"]
    conn = connect(tenancy.get("acme").db_path)
    assert verify_token(conn, value, "2026-11-10T00:00:00Z").labels == {"self"}
    listing = client.get("/internal/admin/tenants/acme/tokens", headers=AUTH).json()
    assert value not in str(listing) and value.split(".")[1] not in str(listing)
    assert listing[0]["id"] == r.json()["id"] and listing[0]["kind"] == "read"


def test_write_tokens_need_exactly_one_source(client, tenancy):
    put(client)
    assert mint(client, kind="write").status_code == 422
    r = mint(client, kind="write", write_source="acme")
    assert r.status_code == 201
    conn = connect(tenancy.get("acme").db_path)
    assert verify_token(conn, r.json()["token"], "2026-11-10T00:00:00Z").writer_sources == {"acme"}


def test_bad_token_requests_are_refused(client):
    put(client)
    assert mint(client, kind="admin").status_code == 422
    assert mint(client, kind="read", labels=["BAD LABEL"]).status_code == 422
    assert mint(client, kind="read", expires_in=5).status_code == 422
    assert mint(client, slug="nope", kind="read").status_code == 404


def test_revoking_stops_the_token_and_is_repeatable(client, tenancy):
    put(client)
    made = mint(client, kind="read").json()
    path = f"/internal/admin/tenants/acme/tokens/{made['id']}"
    assert client.delete(path, headers=AUTH).status_code == 204
    assert client.delete(path, headers=AUTH).status_code == 204
    conn = connect(tenancy.get("acme").db_path)
    assert verify_token(conn, made["token"], "2026-11-10T00:00:00Z") is None


def test_a_tenant_may_hold_only_twenty_live_tokens(client):
    put(client)
    for _ in range(20):
        assert mint(client, kind="read").status_code == 201
    assert mint(client, kind="read").status_code == 409


def test_short_lived_viewer_tokens_do_not_count_toward_the_cap(client):
    put(client)
    for _ in range(20):
        assert mint(client, kind="read").status_code == 201
    assert mint(client, kind="read", expires_in=900, note="viewer").status_code == 201


def test_token_changes_are_recorded_in_the_tenants_own_log(client, tenancy):
    put(client)
    made = mint(client, kind="read", note="phone").json()
    client.delete(f"/internal/admin/tenants/acme/tokens/{made['id']}", headers=AUTH)
    conn = connect(tenancy.get("acme").db_path)
    rows = conn.execute("SELECT source_id, title FROM events ORDER BY seq").fetchall()
    assert [r["source_id"] for r in rows] == ["dayfile"] * 2
    assert made["id"] in rows[0]["title"] and made["token"] not in str(list(map(tuple, rows)))


def test_short_lived_tokens_are_not_written_to_the_log(client, tenancy):
    put(client)
    mint(client, kind="read", expires_in=900, note="viewer")
    conn = connect(tenancy.get("acme").db_path)
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_suspend_resume_and_delete(client, tenancy):
    put(client)
    assert client.post("/internal/admin/tenants/acme/suspend", headers=AUTH).status_code == 204
    assert tenancy.get("acme").suspended is True
    assert client.post("/internal/admin/tenants/acme/resume", headers=AUTH).status_code == 204
    assert tenancy.get("acme").suspended is False
    assert client.delete("/internal/admin/tenants/acme", headers=AUTH).status_code == 204
    assert tenancy.get("acme") is None
    assert client.delete("/internal/admin/tenants/acme", headers=AUTH).status_code == 404


def test_delete_with_erase_leaves_no_trash_copy(client, tenancy):
    put(client)
    r = client.delete("/internal/admin/tenants/acme?erase=true", headers=AUTH)
    assert r.status_code == 204 and tenancy.get("acme") is None
    assert not (tenancy.root / "acme").exists()
    assert not list((tenancy.root / ".trash").glob("acme*"))


def test_writes_are_rate_limited(tenancy, secret_file):
    app = create_app(tenancy=tenancy, admin_secret_file=secret_file, admin_limit=(3, 60))
    c = TestClient(app)
    codes = [put(c, slug=f"t{i}").status_code for i in range(5)]
    assert codes[:3] == [201, 201, 201] and codes[3] == 429


def test_secrets_never_reach_the_logs(client, caplog):
    caplog.set_level(logging.DEBUG)
    put(client)
    value = mint(client, kind="read").json()["token"]
    client.get("/internal/admin/tenants/acme", headers={"Authorization": "Bearer wrong-secret-1"})
    text = caplog.text
    assert SECRET not in text and value not in text and value.split(".")[1] not in text
    assert "wrong-secret-1" not in text


def test_admin_paths_are_not_reachable_with_a_tenant_credential(client, tenancy):
    put(client)
    value = mint(client, kind="write", write_source="acme").json()["token"]
    r = client.get("/internal/admin/tenants/acme", headers={"Authorization": f"Bearer {value}"})
    assert r.status_code == 401


def test_last_use_is_recorded_at_most_hourly(client, tenancy):
    put(client)
    made = mint(client, kind="read").json()
    conn = connect(tenancy.get("acme").db_path)
    assert verify_token(conn, made["token"], "2026-11-10T10:00:00Z")
    assert verify_token(conn, made["token"], "2026-11-10T10:30:00Z")
    assert verify_token(conn, made["token"], "2026-11-10T11:05:00Z")
    row = conn.execute(
        "SELECT last_used FROM token_use WHERE token_id = ?", (made["id"],)
    ).fetchone()
    assert row["last_used"] == "2026-11-10T11:05:00Z"
    listing = client.get("/internal/admin/tenants/acme/tokens", headers=AUTH).json()
    assert listing[0]["last_used"] == "2026-11-10T11:05:00Z"
    skipped = conn.execute("SELECT count(*) FROM token_use").fetchone()[0]
    assert skipped == 1


def put_feed(client, name, slug="acme", **body):
    return client.put(f"/internal/admin/tenants/{slug}/feeds/{name}", json=body, headers=AUTH)


def test_feeds_can_be_created_changed_listed_and_deleted(client, tenancy):
    put(client)
    r = put_feed(client, "work", title="Work", filter={"tag": ["work"]})
    assert r.status_code == 200 and r.json()["visibility"] == "private"
    assert put_feed(client, "work", visibility="public").json()["title"] == "Work"
    listing = client.get("/internal/admin/tenants/acme/feeds", headers=AUTH).json()
    assert [f["name"] for f in listing] == ["default", "work"]
    assert client.delete("/internal/admin/tenants/acme/feeds/work", headers=AUTH).status_code == 204
    assert [
        f["name"] for f in client.get("/internal/admin/tenants/acme/feeds", headers=AUTH).json()
    ] == ["default"]


def test_bad_feed_requests_are_refused(client):
    put(client)
    assert put_feed(client, "Bad_Name").status_code == 422
    assert put_feed(client, "w", filter={"color": "red"}).status_code == 422
    assert put_feed(client, "w", filter=["tag"]).status_code == 422
    assert put_feed(client, "w", extra=1).status_code == 422
    assert put_feed(client, "w", slug="nope").status_code == 404
    r = client.delete("/internal/admin/tenants/acme/feeds/default", headers=AUTH)
    assert r.status_code == 422


def test_feed_routes_need_the_secret(client):
    put(client)
    for method, path in [
        ("put", "/internal/admin/tenants/acme/feeds/w"),
        ("get", "/internal/admin/tenants/acme/feeds"),
        ("delete", "/internal/admin/tenants/acme/feeds/w"),
    ]:
        assert getattr(client, method)(path).status_code == 401


def test_publishing_and_unpublishing_are_recorded_in_the_tenants_log(client, tenancy):
    put(client)
    put_feed(client, "blog", visibility="public")
    put_feed(client, "blog", title="Blog")  # no visibility change: no event
    put_feed(client, "blog", visibility="private")
    conn = connect(tenancy.get("acme").db_path)
    titles = [r["title"] for r in conn.execute("SELECT title FROM events ORDER BY seq")]
    assert titles == ["Feed blog published", "Feed blog made private"]


def test_a_public_feed_made_through_the_api_serves_strangers(tenancy, secret_file):
    app = create_app(tenancy=tenancy, admin_secret_file=secret_file)
    c = TestClient(app, base_url="http://acme.example.test")
    put(c)
    put_feed(c, "blog", visibility="public")
    assert c.get("/f/blog.json").status_code == 200
    put_feed(c, "blog", visibility="private")
    assert c.get("/f/blog.json").status_code == 401


def test_housekeeping_events_stay_out_of_feeds_unless_asked_for(client):
    put(client)
    token = mint(client, kind="read").json()["token"]  # logs "Token ... created (read)"
    tenant = TestClient(client.app, base_url=f"http://acme.{BASE}")
    h = {"Authorization": f"Bearer {token}"}
    assert tenant.get("/feed.json", headers=h).json()["items"] == []
    asked = tenant.get("/feed.json?tag=dayfile-admin", headers=h).json()["items"]
    assert [i["title"] for i in asked][0].startswith("Token ")


def test_export_is_a_gzip_of_a_database_that_passes_its_integrity_check(client, tmp_path):
    import gzip
    import sqlite3

    put(client)
    mint(client, kind="read")
    r = client.get("/internal/admin/tenants/acme/export", headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
    assert r.headers["cache-control"] == "no-store"
    copy = tmp_path / "copy.db"
    copy.write_bytes(gzip.decompress(r.content))
    conn = sqlite3.connect(copy)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] >= 1
    assert client.get("/internal/admin/tenants/nobody/export", headers=AUTH).status_code == 404
    assert client.get("/internal/admin/tenants/acme/export").status_code in (401, 403)


def test_a_users_own_admin_tag_is_not_housekeeping(client):
    put(client)
    writer = mint(client, kind="write", write_source="acme").json()["token"]
    reader = mint(client, kind="read").json()["token"]
    tenant = TestClient(client.app, base_url=f"http://acme.{BASE}")
    r = tenant.post(
        "/events",
        json={"title": "mine", "tags": ["admin"]},
        headers={"Authorization": f"Bearer {writer}"},
    )
    assert r.status_code == 201
    shown = tenant.get(
        "/feed.json?tag=admin", headers={"Authorization": f"Bearer {reader}"}
    ).json()["items"]
    assert [i["title"] for i in shown] == ["mine"]


def test_heartbeats_can_be_saved_listed_and_deleted_and_mistakes_are_refused(client):
    put(client)
    base = "/internal/admin/tenants/acme/heartbeats"
    r = client.put(f"{base}/backup", json={"every": "24h", "source": "backup-script"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["every"] == "24h"
    assert [h["name"] for h in client.get(base, headers=AUTH).json()] == ["backup"]
    for body in ({"every": "soon", "tag": "x"}, {"every": "1h"}, {"every": "1h", "color": "red"}):
        assert client.put(f"{base}/bad", json=body, headers=AUTH).status_code == 422
    assert client.delete(f"{base}/backup", headers=AUTH).status_code == 204
    assert client.get(base, headers=AUTH).json() == []
    assert client.get(base, headers={}).status_code in (401, 403)
