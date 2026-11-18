"""The hosted MCP endpoint: credential gate, discovery document, tools run as the caller."""

import json

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.auth.tokens import create_token
from dayfile.db import connect

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
