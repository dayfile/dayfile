# ruff: noqa: F811  (fixtures imported from test_dashboard are redefined as arguments)
"""dayfile.app as an authorization server for assistants, checked end to end through the MCP URL."""

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from test_dashboard import (  # noqa: F401 - fixtures
    ORIGIN,
    POST,
    SECRET,
    control,
    core,
    db,
    gh,
    host,
    tenancy,
    web,
    with_log,
)

VERIFIER = "v" * 43
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
)
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
JSON_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def register(web, redirect=REDIRECT, name="Claude"):
    r = web.post("/oauth/register", json={"client_name": name, "redirect_uris": [redirect]})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def authorize_params(client_id, redirect=REDIRECT, **over):
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "xyz",
        **over,
    }


def connect(web, client_id, access="write", redirect=REDIRECT):
    """Approve a connection as the signed-in owner; return the authorization code."""
    fields = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "code_challenge": CHALLENGE,
        "state": "xyz",
        "decision": "allow",
        "access": access,
    }
    r = web.post("/oauth/authorize", data=fields, headers=POST)
    assert r.status_code == 303, r.text
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert query["state"] == ["xyz"] and query["iss"] == [ORIGIN]
    return query["code"][0]


def exchange(web, client_id, code, verifier=VERIFIER, redirect=REDIRECT):
    return web.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect,
            "code_verifier": verifier,
        },
    )


def refresh(web, client_id, token):
    return web.post(
        "/oauth/token",
        data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": token},
    )


@pytest.fixture
def signed_in(web, db):
    with_log(web, db)
    return web


@pytest.fixture
def mcp_core(core, tenancy, tmp_path):
    app = create_app(
        tenancy=tenancy,
        admin_secret_file=tmp_path / "admin_secret",
        clock=lambda: "2026-11-10T09:00:00Z",
        mcp=True,
        oauth_issuer=ORIGIN,
    )
    with TestClient(app, base_url="http://acme.example.test") as client:
        yield client


def mcp(client, credential, name=None, **arguments):
    headers = {**JSON_HEADERS, "Authorization": f"Bearer {credential}"}
    if name is None:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    else:
        params = {"name": name, "arguments": arguments}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    return client.post("/mcp", json=body, headers=headers)


# --- discovery and registration ---


def test_server_metadata_advertises_pkce_public_clients_and_registration(web):
    doc = web.get("/.well-known/oauth-authorization-server").json()
    assert doc["issuer"] == ORIGIN
    assert doc["authorization_endpoint"] == ORIGIN + "/oauth/authorize"
    assert doc["token_endpoint"] == ORIGIN + "/oauth/token"
    assert doc["registration_endpoint"] == ORIGIN + "/oauth/register"
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]
    assert "client_id_metadata_document_supported" not in doc


@pytest.mark.parametrize(
    "uri",
    [
        "http://evil.example/cb",
        "javascript:alert(1)",
        "https://claude.ai/cb#frag",
        "https://user:pw@claude.ai/cb",
        "cursor://app/callback",
        "https://claude.ai/ cb",
        "https://ex;ample.com/cb",
    ],
)
def test_registration_refuses_unsafe_redirects(web, uri):
    r = web.post("/oauth/register", json={"client_name": "x", "redirect_uris": [uri]})
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"


def test_registration_accepts_https_and_loopback_and_only_public_clients(web):
    ok = web.post(
        "/oauth/register",
        json={"redirect_uris": ["http://localhost:3118/callback", REDIRECT], "client_name": "<b>X"},
    )
    assert ok.status_code == 201 and ok.json()["client_name"] == "bX"
    assert ok.json()["token_endpoint_auth_method"] == "none"
    secret = web.post(
        "/oauth/register",
        json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_post"},
    )
    assert secret.status_code == 400 and secret.json()["error"] == "invalid_client_metadata"
    assert web.post("/oauth/register", content=b"nope").status_code == 400


# --- the consent step ---


def test_an_anonymous_visitor_is_sent_to_sign_in_and_brought_back(web):
    client_id = register(web)
    r = web.get("/oauth/authorize", params=authorize_params(client_id))
    assert r.status_code == 303 and r.headers["location"] == "/login?connect=1"
    assert "dayfile-return" in r.headers["set-cookie"]
    assert "/oauth/authorize?" in r.headers["set-cookie"]


def test_the_consent_page_names_the_app_the_log_and_where_it_returns(signed_in):
    client_id = register(signed_in)
    r = signed_in.get("/oauth/authorize", params=authorize_params(client_id))
    assert r.status_code == 200
    assert "Claude" in r.text and "acme.example.test" in r.text and "claude.ai" in r.text
    assert "form-action 'self' https://claude.ai" in r.headers["content-security-policy"]
    assert "script-src" not in r.headers["content-security-policy"]


def test_an_app_returning_somewhere_unknown_is_flagged_as_unverified(signed_in):
    shady = register(signed_in, redirect="https://evil.example/cb", name="Claude")
    page = signed_in.get(
        "/oauth/authorize", params=authorize_params(shady, "https://evil.example/cb")
    )
    assert "cannot verify this app" in page.text and "evil.example" in page.text
    claude = register(signed_in)
    assert (
        "cannot verify"
        not in signed_in.get("/oauth/authorize", params=authorize_params(claude)).text
    )
    local = register(signed_in, redirect="http://localhost:3118/callback", name="Claude Code")
    assert (
        "running on this computer"
        in signed_in.get(
            "/oauth/authorize", params=authorize_params(local, "http://localhost:3118/callback")
        ).text
    )


def test_an_unregistered_client_or_redirect_never_redirects(signed_in):
    client_id = register(signed_in)
    bad = authorize_params(client_id, redirect="https://evil.example/cb")
    r = signed_in.get("/oauth/authorize", params=bad)
    assert r.status_code == 400 and "location" not in r.headers
    r = signed_in.get("/oauth/authorize", params=authorize_params("dfc_unknown"))
    assert r.status_code == 400 and "location" not in r.headers


def test_a_request_without_pkce_is_refused_to_the_client(signed_in):
    client_id = register(signed_in)
    params = authorize_params(client_id, code_challenge_method="plain")
    r = signed_in.get("/oauth/authorize", params=params)
    assert r.status_code == 303
    assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["invalid_request"]


def test_a_resource_for_another_log_is_refused(signed_in):
    client_id = register(signed_in)
    other = authorize_params(client_id, resource="https://someone-else.example.test/mcp")
    r = signed_in.get("/oauth/authorize", params=other)
    assert r.status_code == 400 and "Wrong account" in r.text
    mine = authorize_params(client_id, resource="https://acme.example.test/mcp")
    assert signed_in.get("/oauth/authorize", params=mine).status_code == 200


def test_the_main_address_is_a_valid_resource(signed_in):
    client_id = register(signed_in)
    main = authorize_params(client_id, resource="https://example.test/mcp")
    assert signed_in.get("/oauth/authorize", params=main).status_code == 200
    fields = {**main, "decision": "allow", "access": "write"}
    r = signed_in.post("/oauth/authorize", data=fields, headers=POST)
    assert "code" in parse_qs(urlsplit(r.headers["location"]).query)
    fields["resource"] = "https://other.test/mcp"
    r = signed_in.post("/oauth/authorize", data=fields, headers=POST)
    assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["invalid_request"]


def test_denying_returns_access_denied_and_the_post_needs_the_site_as_origin(signed_in):
    client_id = register(signed_in)
    fields = {"client_id": client_id, "redirect_uri": REDIRECT, "state": "s1", "decision": "deny"}
    r = signed_in.post("/oauth/authorize", data=fields, headers=POST)
    assert parse_qs(urlsplit(r.headers["location"]).query)["error"] == ["access_denied"]
    forged = signed_in.post(
        "/oauth/authorize", data=fields, headers={"Origin": "https://evil.example"}
    )
    assert forged.status_code == 403


# --- tokens ---


def test_the_code_flow_gives_a_credential_that_works_at_the_mcp_url(signed_in, mcp_core):
    client_id = register(signed_in)
    r = exchange(signed_in, client_id, connect(signed_in, client_id))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600 and body["refresh_token"]
    assert "~" in body["access_token"]

    added = mcp(mcp_core, body["access_token"], "dayfile_add_event", title="from claude", todo=True)
    assert not added.json()["result"].get("isError"), added.text
    listed = mcp(mcp_core, body["access_token"], "dayfile_list_events", status="open")
    assert "from claude" in listed.text


def test_the_credential_names_the_log_so_the_main_address_can_use_it(signed_in, mcp_core):
    client_id = register(signed_in)
    body = exchange(signed_in, client_id, connect(signed_in, client_id)).json()
    assert body["access_token"].startswith("acme:")
    headers = {
        **JSON_HEADERS,
        "Authorization": f"Bearer {body['access_token']}",
        "Host": "example.test",
    }
    call = {"name": "dayfile_add_event", "arguments": {"title": "via the main address"}}
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": call}
    added = mcp_core.post("/mcp", json=rpc, headers=headers)
    assert not added.json()["result"].get("isError"), added.text
    own = mcp(mcp_core, body["access_token"], "dayfile_list_events")  # the log's own address
    assert "via the main address" in own.text


def test_a_read_only_approval_cannot_write(signed_in, mcp_core):
    client_id = register(signed_in)
    body = exchange(signed_in, client_id, connect(signed_in, client_id, access="read")).json()
    assert "~" not in body["access_token"]
    assert (
        not mcp(mcp_core, body["access_token"], "dayfile_list_events")
        .json()["result"]
        .get("isError")
    )
    refused = mcp(mcp_core, body["access_token"], "dayfile_add_event", title="nope")
    assert refused.json()["result"]["isError"] and "read-only" in refused.text


def test_a_code_works_once_and_only_for_its_client_redirect_and_verifier(signed_in):
    client_id = register(signed_in)
    other = register(signed_in, name="Other")
    cases = [
        {"verifier": "w" * 43},
        {"redirect": "https://claude.ai/other"},
        {"client": other},
    ]
    for case in cases:
        code = connect(signed_in, client_id)
        r = exchange(
            signed_in,
            case.get("client", client_id),
            code,
            verifier=case.get("verifier", VERIFIER),
            redirect=case.get("redirect", REDIRECT),
        )
        assert r.status_code == 400 and r.json()["error"] == "invalid_grant", case
        assert exchange(signed_in, client_id, code).status_code == 400  # spent by the attempt
    code = connect(signed_in, client_id)
    assert exchange(signed_in, client_id, code).status_code == 200
    assert exchange(signed_in, client_id, code).status_code == 400


def test_an_unknown_client_is_invalid_client(signed_in):
    r = exchange(signed_in, "dfc_nope", "code")
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_refresh_rotates_and_the_old_access_token_stops_working(signed_in, mcp_core):
    client_id = register(signed_in)
    first = exchange(signed_in, client_id, connect(signed_in, client_id)).json()
    second = refresh(signed_in, client_id, first["refresh_token"]).json()
    assert second["refresh_token"] != first["refresh_token"]
    assert mcp(mcp_core, second["access_token"]).status_code == 200
    assert mcp(mcp_core, first["access_token"]).status_code == 401  # revoked on rotation


def test_replaying_a_replaced_refresh_token_ends_the_whole_connection(signed_in, mcp_core):
    client_id = register(signed_in)
    first = exchange(signed_in, client_id, connect(signed_in, client_id)).json()
    second = refresh(signed_in, client_id, first["refresh_token"]).json()
    replay = refresh(signed_in, client_id, first["refresh_token"])
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert refresh(signed_in, client_id, second["refresh_token"]).status_code == 400
    assert mcp(mcp_core, second["access_token"]).status_code == 401
    assert "Connected assistants" not in signed_in.get("/account/tokens").text


def test_a_refresh_token_belongs_to_one_client(signed_in):
    client_id = register(signed_in)
    other = register(signed_in, name="Other")
    first = exchange(signed_in, client_id, connect(signed_in, client_id)).json()
    assert refresh(signed_in, other, first["refresh_token"]).status_code == 400
    assert refresh(signed_in, client_id, first["refresh_token"]).status_code == 200


# --- managing connections ---


def test_connections_are_listed_without_their_tokens_and_can_be_disconnected(signed_in, mcp_core):
    client_id = register(signed_in)
    body = exchange(signed_in, client_id, connect(signed_in, client_id)).json()
    page = signed_in.get("/account/tokens").text
    assert "Connected assistants" in page and "Claude" in page and "Read and write" in page
    assert "connected app" not in page  # the plumbing tokens are not listed as tokens
    grant_id = page.split("/account/connections/")[1].split("/")[0]

    assert signed_in.post(f"/account/connections/{grant_id}/disconnect").status_code == 403
    r = signed_in.post(f"/account/connections/{grant_id}/disconnect", headers=POST)
    assert r.status_code == 303
    assert mcp(mcp_core, body["access_token"]).status_code == 401
    assert refresh(signed_in, client_id, body["refresh_token"]).status_code == 400
    assert "Connected assistants" not in signed_in.get("/account/tokens").text


def test_deleting_the_account_ends_connections(signed_in, db):
    client_id = register(signed_in)
    body = exchange(signed_in, client_id, connect(signed_in, client_id)).json()
    r = signed_in.post("/account/delete", data={"confirm": "acme"}, headers=POST)
    assert r.status_code in (200, 303)
    assert refresh(signed_in, client_id, body["refresh_token"]).status_code == 400


def test_registration_is_rate_limited(web):
    body = {"client_name": "x", "redirect_uris": [REDIRECT]}
    codes = [web.post("/oauth/register", json=body).status_code for _ in range(62)]
    assert codes[:60] == [201] * 60 and codes[60:] == [429, 429]
