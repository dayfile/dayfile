"""The hosted MCP endpoint: credential gate, discovery document, tools run as the caller."""

import json

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.auth.tokens import create_token
from dayfile.db import connect
from dayfile.tenancy import DirectoryTenancy

ISSUER = "https://dayfile.app"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(method, params=None, id=1):
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}


@pytest.fixture
def tokens(tmp_path):
    conn = connect(tmp_path / "d.db")
    out = {
        "read": create_token(conn, ["self"]).value,
        "write": create_token(conn, ["self"], write_sources=["mcp"]).value,
        "other_read": create_token(conn, ["family"]).value,
    }
    conn.close()
    return out


@pytest.fixture
def client(tmp_path, tokens):
    app = create_app(tmp_path / "d.db", mcp=True, oauth_issuer=ISSUER)
    with TestClient(app) as c:
        yield c


def call(client, credential, method, params=None, id=1):
    headers = {**HEADERS, "Authorization": f"Bearer {credential}"} if credential else HEADERS
    return client.post("/mcp", json=rpc(method, params, id), headers=headers)


def tool(client, credential, name, **arguments):
    r = call(client, credential, "tools/call", {"name": name, "arguments": arguments})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_no_credential_gets_a_challenge_that_names_the_metadata(client):
    r = call(client, None, "tools/list")
    assert r.status_code == 401
    challenge = r.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"' in (
        challenge
    )


def test_a_wrong_or_write_only_credential_is_rejected(client, tokens):
    assert call(client, "nope.nope", "tools/list").status_code == 401
    r = call(client, tokens["write"], "tools/list")
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]


def test_metadata_names_the_resource_and_the_authorization_server(client):
    r = client.get("/.well-known/oauth-protected-resource/mcp")
    assert r.status_code == 200
    assert r.json() == {
        "resource": "http://testserver/mcp",
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
    }


def test_without_an_issuer_there_is_no_metadata_and_no_resource_hint(tmp_path, tokens):
    app = create_app(tmp_path / "d.db", mcp=True)
    with TestClient(app) as c:
        assert c.get("/.well-known/oauth-protected-resource/mcp").status_code == 404
        assert "resource_metadata" not in call(c, None, "tools/list").headers["www-authenticate"]


def test_tools_list_and_a_full_write_read_cycle_with_both_tokens(client, tokens):
    credential = f"{tokens['read']}~{tokens['write']}"
    listing = call(client, credential, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in listing} >= {"dayfile_add_event", "dayfile_list_events"}
    added = tool(client, credential, "dayfile_add_event", title="from mcp", todo=True)
    assert not added.get("isError")
    found = tool(client, credential, "dayfile_list_events", tags=["todo"], status="open")
    assert "from mcp" in json.dumps(found)


def test_a_read_only_credential_can_read_but_not_write(client, tokens):
    read_only = tokens["read"]
    assert not tool(client, read_only, "dayfile_list_events").get("isError")
    refused = tool(client, read_only, "dayfile_add_event", title="nope")
    assert refused["isError"] and "read-only" in json.dumps(refused)


def test_the_write_part_must_be_a_real_write_token(client, tokens):
    credential = f"{tokens['read']}~{tokens['other_read']}"  # a read token in the write slot
    refused = tool(client, credential, "dayfile_add_event", title="nope")
    assert refused["isError"]


def test_each_request_runs_as_its_own_caller(client, tokens):
    mine = f"{tokens['read']}~{tokens['write']}"
    tool(client, mine, "dayfile_add_event", title="private note")
    others = tool(client, tokens["other_read"], "dayfile_list_events")
    assert "private note" not in json.dumps(others)


def test_mcp_is_off_unless_asked_for(tmp_path):
    with TestClient(create_app(tmp_path / "d.db")) as c:
        assert c.post("/mcp", json=rpc("tools/list"), headers=HEADERS).status_code == 404


# --- the main address: the log comes from the username in the credential ---

BASE = "example.test"


@pytest.fixture
def hosted(tmp_path):
    """Two logs under one base domain, each with a read and a write token."""
    tenancy = DirectoryTenancy(tmp_path / "tenants", BASE)
    creds = {}
    for name in ("acme", "beta"):
        tenant = tenancy.create(name)
        conn = connect(tenant.db_path)
        creds[name] = {
            "read": create_token(conn, ["self"]).value,
            "write": create_token(conn, ["self"], write_sources=["mcp"]).value,
        }
        conn.close()
    app = create_app(tenancy=tenancy, mcp=True, oauth_issuer=ISSUER)
    with TestClient(app) as c:
        yield c, creds


def main_call(client, credential, method="tools/list", params=None, host=BASE):
    headers = {**HEADERS, "Host": host}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    return client.post("/mcp", json=rpc(method, params), headers=headers)


def main_tool(client, credential, name, **arguments):
    r = main_call(client, credential, "tools/call", {"name": name, "arguments": arguments})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_the_main_address_challenges_and_names_its_own_metadata(hosted):
    client, _ = hosted
    r = main_call(client, None)
    assert r.status_code == 401
    assert (
        f'resource_metadata="https://{BASE}/.well-known/oauth-protected-resource/mcp"'
        in (r.headers["www-authenticate"])
    )
    meta = client.get("/.well-known/oauth-protected-resource/mcp", headers={"Host": BASE})
    assert meta.json() == {
        "resource": f"https://{BASE}/mcp",
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
    }


def test_the_main_address_acts_on_the_log_named_in_the_credential(hosted):
    client, creds = hosted
    acme = f"acme:{creds['acme']['read']}~{creds['acme']['write']}"
    beta = f"beta:{creds['beta']['read']}~{creds['beta']['write']}"
    main_tool(client, acme, "dayfile_add_event", title="acme only")
    assert "acme only" in json.dumps(main_tool(client, acme, "dayfile_list_events"))
    assert "acme only" not in json.dumps(main_tool(client, beta, "dayfile_list_events"))


def test_a_read_only_credential_at_the_main_address_cannot_write(hosted):
    client, creds = hosted
    result = main_tool(client, f"acme:{creds['acme']['read']}", "dayfile_add_event", title="nope")
    assert result["isError"] is True and "read-only" in json.dumps(result)


def test_the_main_address_refuses_a_missing_wrong_or_borrowed_username(hosted):
    client, creds = hosted
    read = creds["acme"]["read"]
    for credential in (read, f"nobody:{read}", f"beta:{read}", f"www:{read}", "acme:nope.nope"):
        r = main_call(client, credential)
        assert r.status_code == 401, credential
        assert 'error="invalid_token"' in r.headers["www-authenticate"]


def test_a_credential_with_a_username_also_works_at_that_logs_own_address(hosted):
    client, creds = hosted
    own = f"acme:{creds['acme']['read']}"
    assert main_call(client, own, host=f"acme.{BASE}").status_code == 200
    assert main_call(client, creds["acme"]["read"], host=f"acme.{BASE}").status_code == 200
    r = main_call(client, f"beta:{creds['beta']['read']}", host=f"acme.{BASE}")
    assert r.status_code == 401  # another log's username on this log's address


def test_a_suspended_log_is_refused_at_the_main_address(hosted, tmp_path):
    client, creds = hosted
    DirectoryTenancy(tmp_path / "tenants", BASE).update("acme", suspended=True)
    assert main_call(client, f"acme:{creds['acme']['read']}").status_code == 403


def test_without_a_directory_of_logs_there_is_no_main_address(client, tokens):
    r = call(client, f"acme:{tokens['read']}", "tools/list")
    assert r.status_code == 401
