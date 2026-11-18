import json

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.auth.tokens import create_token
from dayfile.db import connect
from dayfile.tenancy import DirectoryTenancy, SingleTenant, TenancyError

BASE = "example.test"
NOW = "2026-11-09T17:00:00Z"


def ev(n, source="a", **over):
    base = {
        "specversion": "dayfile/1",
        "id": f"01JC8Z4Q7M2X9K3T5W6N8R{n:04d}",
        "source": {"id": source, "ref": f"r{n}"},
        "occurred_at": "2026-11-06T10:00:00Z",
        "title": f"event {n}",
    }
    base.update(over)
    return base


@pytest.fixture
def tenancy(tmp_path):
    return DirectoryTenancy(tmp_path / "tenants", BASE)


@pytest.fixture
def app(tenancy):
    return create_app(tenancy=tenancy, clock=lambda: NOW)


def client_for(app, slug, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, base_url=f"http://{slug}.{BASE}", headers=headers)


def mint(tenancy, slug, *, write=None, labels=("self",)):
    tenant = tenancy.resolve(f"{slug}.{BASE}")
    conn = connect(tenant.db_path)
    try:
        return create_token(conn, list(labels), write_sources=write).value
    finally:
        conn.close()


@pytest.fixture
def two(tenancy, app):
    """Tenants acme and globex, each with a write token and a read token."""
    out = {}
    for slug in ("acme", "globex"):
        tenancy.create(slug)
        out[slug] = (
            client_for(app, slug, mint(tenancy, slug, write=["a"])),
            client_for(app, slug, mint(tenancy, slug)),
        )
    return out


# --- resolution ----------------------------------------------------------------


def test_create_makes_a_private_directory_with_a_ready_database(tenancy):
    tenant = tenancy.create("acme")
    assert tenant.db_path.is_file()
    assert (tenant.db_path.parent.stat().st_mode & 0o777) == 0o700
    conn = connect(tenant.db_path)
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize(
    "bad", ["", "Acme", "a.b", "-a", "a-", "a_b", "x" * 33, "../x", "www", "api"]
)
def test_bad_or_reserved_slugs_are_rejected(tenancy, bad):
    with pytest.raises(TenancyError):
        tenancy.create(bad)


def test_duplicate_slug_is_rejected(tenancy):
    tenancy.create("acme")
    with pytest.raises(TenancyError, match="exists"):
        tenancy.create("acme")


@pytest.mark.parametrize(
    "host",
    [
        "acme.example.test",
        "ACME.example.test",
        "acme.example.test:8443",
    ],
)
def test_host_resolves_to_the_tenant(tenancy, host):
    tenancy.create("acme")
    assert tenancy.resolve(host).id == "acme"


@pytest.mark.parametrize(
    "host",
    [
        "",
        "example.test",
        "nobody.example.test",
        "a.b.example.test",
        "acme.other.test",
        "acme.example.test.evil.test",
        "..example.test",
    ],
)
def test_other_hosts_resolve_to_nothing(tenancy, host):
    tenancy.create("acme")
    assert tenancy.resolve(host) is None


def test_list_and_usage(tenancy, two):
    assert tenancy.list() == ["acme", "globex"]
    two["acme"][0].post("/events", json=ev(1))
    usage = tenancy.usage("acme")
    assert usage["events"] == 1 and usage["bytes"] > 0
    assert tenancy.usage("globex")["events"] == 0


# --- isolation -------------------------------------------------------------------


def test_each_tenant_sees_only_its_own_events(two):
    assert two["acme"][0].post("/events", json=ev(1)).status_code == 201
    assert [e["title"] for e in two["acme"][1].get("/events").json()["events"]] == ["event 1"]
    assert two["globex"][1].get("/events").json()["events"] == []


def test_a_token_from_one_tenant_is_useless_on_another(tenancy, app, two):
    acme_read = mint(tenancy, "acme")
    assert client_for(app, "acme", acme_read).get("/events").status_code == 200
    assert client_for(app, "globex", acme_read).get("/events").status_code == 401
    assert client_for(app, "globex", acme_read).get("/feed.json").status_code == 401
    path = client_for(app, "globex").get(f"/t/{acme_read}/feed.json")
    assert path.status_code == 401


def test_a_write_token_from_one_tenant_cannot_write_to_another(tenancy, app, two):
    acme_write = mint(tenancy, "acme", write=["a"])
    r = client_for(app, "globex", acme_write).post("/events", json=ev(1))
    assert r.status_code == 401
    assert two["globex"][1].get("/events").json()["events"] == []


def test_same_event_id_can_exist_in_two_tenants(two):
    for slug in ("acme", "globex"):
        assert two[slug][0].post("/events", json=ev(1)).status_code == 201


def test_etags_differ_between_tenants(two):
    for slug in ("acme", "globex"):
        two[slug][0].post("/events", json=ev(1))
    tags = {slug: two[slug][1].get("/events").headers["etag"] for slug in two}
    assert tags["acme"] != tags["globex"]


def test_unknown_tenant_is_404_everywhere(app):
    nobody = client_for(app, "nobody", "x.y")
    for path in ("/events", "/events/abc", "/events/stream", "/feed.json", "/t/x.y/feed.atom"):
        r = nobody.get(path)
        assert r.status_code == 404 and r.json() == {"error": "unknown tenant"}, path
    assert nobody.post("/events", json=ev(1)).status_code == 404


def test_the_bare_base_domain_is_not_a_tenant(app):
    r = TestClient(app, base_url=f"http://{BASE}").get("/events")
    assert r.status_code == 404


# --- suspension, limits, quotas ---------------------------------------------------


def test_suspended_tenant_is_refused_and_resuming_restores_it(tenancy, two):
    write, read = two["acme"]
    write.post("/events", json=ev(1))
    tenancy.update("acme", suspended=True)
    for r in (read.get("/events"), read.get("/feed.json"), write.post("/events", json=ev(2))):
        assert r.status_code == 403 and r.json() == {"error": "account suspended"}
    assert two["globex"][1].get("/events").status_code == 200  # others unaffected
    tenancy.update("acme", suspended=False)
    assert [e["title"] for e in read.get("/events").json()["events"]] == ["event 1"]


def test_suspension_does_not_touch_the_data(tenancy, two):
    two["acme"][0].post("/events", json=ev(1))
    before = tenancy.usage("acme")["events"]
    tenancy.update("acme", suspended=True)
    assert tenancy.usage("acme")["events"] == before == 1


def test_per_tenant_write_limit_overrides_the_default(tenancy, two):
    tenancy.update("acme", write_limit=(2, 60))
    write = two["acme"][0]
    assert write.post("/events", json=[ev(1), ev(2)]).status_code == 201
    r = write.post("/events", json=ev(3))
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    other = two["globex"][0]  # default limit is far higher
    assert other.post("/events", json=[ev(n) for n in range(1, 10)]).status_code == 201


def test_write_windows_are_not_shared_between_tenants(tenancy, app, two):
    tenancy.update("acme", write_limit=(1, 60))
    tenancy.update("globex", write_limit=(1, 60))
    assert two["acme"][0].post("/events", json=ev(1)).status_code == 201
    assert two["globex"][0].post("/events", json=ev(1)).status_code == 201


def test_event_quota_refuses_what_would_exceed_it_and_writes_nothing(tenancy, two):
    tenancy.update("acme", max_events=3)
    write = two["acme"][0]
    assert write.post("/events", json=[ev(1), ev(2)]).status_code == 201
    r = write.post("/events", json=[ev(3), ev(4)])
    assert r.status_code == 403 and r.json() == {"error": "event quota exceeded"}
    assert tenancy.usage("acme")["events"] == 2
    assert write.post("/events", json=ev(3)).status_code == 201  # exactly at the limit
    assert write.post("/events", json=ev(4)).status_code == 403


def test_reads_still_work_when_the_quota_is_full(tenancy, two):
    tenancy.update("acme", max_events=1)
    two["acme"][0].post("/events", json=ev(1))
    assert two["acme"][1].get("/events").status_code == 200


def test_usage_reports_this_logs_own_numbers_to_a_read_token_only(tenancy, two):
    tenancy.update("acme", max_events=10, max_bytes=10**6)
    write, read = two["acme"]
    write.post("/events", json=ev(1))
    r = read.get("/usage")
    body = r.json()
    assert r.status_code == 200 and body["events"] == 1 and body["max_events"] == 10
    assert body["max_bytes"] == 10**6 and body["bytes"] > 0
    assert "private" in r.headers["Cache-Control"] and r.headers["Vary"] == "Authorization"
    assert write.get("/usage").status_code in (401, 403)
    assert two["globex"][1].get("/usage").json()["events"] == 0


def test_unknown_limit_keys_and_bad_values_are_rejected(tenancy):
    tenancy.create("acme")
    with pytest.raises(TenancyError):
        tenancy.update("acme", plan="gold")
    with pytest.raises(TenancyError):
        tenancy.update("acme", max_events=-1)
    with pytest.raises(TenancyError):
        tenancy.update("acme", write_limit=(0, 60))
    with pytest.raises(TenancyError):
        tenancy.update("nobody", suspended=True)


def test_a_corrupt_tenant_file_fails_closed(tenancy, two):
    (tenancy.root / "acme" / "tenant.json").write_text("{not json")
    r = two["acme"][1].get("/events")
    assert r.status_code == 403 and r.json() == {"error": "account suspended"}


def test_limits_are_read_from_a_file_a_control_plane_can_write(tenancy, two):
    meta = tenancy.root / "acme" / "tenant.json"
    meta.write_text(json.dumps({"suspended": False, "max_events": 1}))
    write = two["acme"][0]
    assert write.post("/events", json=ev(1)).status_code == 201
    assert write.post("/events", json=ev(2)).status_code == 403


# --- deletion --------------------------------------------------------------------


def test_delete_moves_the_data_to_the_trash_and_frees_the_name(tenancy, two):
    two["acme"][0].post("/events", json=ev(1))
    trash = tenancy.delete("acme")
    assert (trash / "dayfile.db").is_file() and trash.parent.name == ".trash"
    assert tenancy.resolve(f"acme.{BASE}") is None
    assert two["acme"][1].get("/events").status_code == 404
    assert tenancy.list() == ["globex"]
    tenancy.create("acme")  # a new, empty tenant under the same name
    assert tenancy.usage("acme")["events"] == 0


# --- request size ----------------------------------------------------------------


def test_oversized_body_is_refused_with_413(tmp_path):
    path = tmp_path / "d.db"
    conn = connect(path)
    token = create_token(conn, ["self"], write_sources=["a"]).value
    conn.close()
    client = TestClient(
        create_app(path, max_body_bytes=500), headers={"Authorization": f"Bearer {token}"}
    )
    assert client.post("/events", json=ev(1)).status_code == 201
    big = ev(2, body="x" * 600)
    r = client.post("/events", json=big)
    assert r.status_code == 413 and "too large" in r.json()["error"]
    chunked = client.post("/events", content=iter([b'{"a":"', b"x" * 600, b'"}']))
    assert chunked.status_code == 413  # no Content-Length to trust


def test_default_body_limit_is_one_mebibyte(tmp_path):
    path = tmp_path / "d.db"
    conn = connect(path)
    token = create_token(conn, ["self"], write_sources=["a"]).value
    conn.close()
    client = TestClient(create_app(path), headers={"Authorization": f"Bearer {token}"})
    r = client.post("/events", content=b" " * (1024 * 1024 + 1))
    assert r.status_code == 413


# --- health and wiring -------------------------------------------------------------


def test_healthz_does_not_need_a_tenant_host(app, tenancy):
    tenancy.create("acme")
    r = TestClient(app, base_url="http://anything.invalid").get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_healthz_is_503_when_the_tenant_root_is_missing(tmp_path):
    app = create_app(tenancy=DirectoryTenancy(tmp_path / "missing", BASE))
    assert TestClient(app).get("/healthz").status_code == 503


def test_environment_selects_directory_tenancy(tmp_path, monkeypatch):
    root = tmp_path / "t"
    DirectoryTenancy(root, BASE).create("acme")
    monkeypatch.setenv("DAYFILE_TENANTS_DIR", str(root))
    monkeypatch.setenv("DAYFILE_BASE_DOMAIN", BASE)
    client = TestClient(create_app(), base_url=f"http://acme.{BASE}")
    assert client.get("/events").status_code == 401  # tenant found, credential missing
    assert TestClient(create_app(), base_url=f"http://zzz.{BASE}").get("/events").status_code == 404


def test_tenants_dir_without_a_base_domain_is_a_configuration_error(tmp_path, monkeypatch):
    monkeypatch.setenv("DAYFILE_TENANTS_DIR", str(tmp_path))
    monkeypatch.delenv("DAYFILE_BASE_DOMAIN", raising=False)
    with pytest.raises(ValueError, match="DAYFILE_BASE_DOMAIN"):
        create_app()


def test_single_tenant_serves_every_host(tmp_path):
    path = tmp_path / "d.db"
    connect(path).close()
    assert SingleTenant(path).resolve("whatever.example").db_path == path
    assert SingleTenant(path).resolve("").id == "default"


# --- TLS permission hook (Caddy on-demand TLS `ask`) -------------------------------


def test_tls_ask_allows_only_live_tenant_hostnames(tenancy, app):
    tenancy.create("acme")
    tenancy.create("gone")
    tenancy.update("gone", suspended=True)
    anyhost = TestClient(app, base_url="http://dayfile:8000")
    ask = lambda d: anyhost.get("/internal/tls-ask", params={"domain": d}).status_code  # noqa: E731
    assert ask(f"acme.{BASE}") == 200
    assert ask(f"nobody.{BASE}") == 404
    assert ask(f"gone.{BASE}") == 404  # no certificates for suspended accounts
    assert ask(BASE) == 404 and ask("evil.test") == 404 and ask("") == 404


def test_tls_ask_is_closed_for_a_single_tenant_deployment(tmp_path):
    path = tmp_path / "d.db"
    connect(path).close()
    r = TestClient(create_app(path)).get("/internal/tls-ask", params={"domain": "any.example"})
    assert r.status_code == 404


def test_tls_ask_says_nothing_in_its_body(tenancy, app):
    tenancy.create("acme")
    r = TestClient(app).get("/internal/tls-ask", params={"domain": f"acme.{BASE}"})
    assert r.content == b"" and r.headers["cache-control"] == "no-store"


def test_purge_removes_only_deletions_old_enough_and_nothing_else(tmp_path):
    from dayfile.tenancy import DirectoryTenancy, purge_trash

    tenancy = DirectoryTenancy(tmp_path, "example.test")
    for slug in ("old-one", "fresh"):
        tenancy.create(slug)
        tenancy.delete(slug)
    trash = tmp_path / ".trash"
    (old,) = [p for p in trash.iterdir() if p.name.startswith("old-one")]
    old.rename(trash / "old-one-2026-01-01T000000Z")
    (trash / "stranger").mkdir()
    gone = purge_trash(tmp_path, 30, now="2026-11-12T00:00:00Z")
    assert gone == ["old-one-2026-01-01T000000Z"]
    assert (trash / "stranger").exists() and any(
        p.name.startswith("fresh") for p in trash.iterdir()
    )
    assert purge_trash(tmp_path / "nowhere", 30) == []


def test_storage_quota_refuses_new_events_once_the_database_is_full(tenancy, two):
    write, read = two["acme"]
    tenancy.update("acme", max_bytes=10**9)
    assert write.post("/events", json=ev(1)).status_code == 201
    tenancy.update("acme", max_bytes=1)
    r = write.post("/events", json=ev(2))
    assert r.status_code == 403 and r.json() == {"error": "storage quota exceeded"}
    assert read.get("/events").status_code == 200  # reading and exporting still work
    with pytest.raises(TenancyError):
        tenancy.update("acme", max_bytes=-5)
