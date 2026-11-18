import logging
import socket
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.auth.tokens import create_token, list_tokens, revoke_token, verify_token
from dayfile.db import connect
from dayfile.logsafe import RedactTokens
from helpers import authed_client

ATOM = "{http://www.w3.org/2005/Atom}"


class Clock:
    def __init__(self, now="2026-11-06T12:00:00Z"):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "d.db"


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


def ev(n, audience=None, **over):
    base = {
        "specversion": "dayfile/1",
        "id": f"01JC8Z4Q7M2X9K3T5W6N8R{n:04d}",
        "source": {"id": "test", "ref": f"r{n}"},
        "occurred_at": "2026-11-06T10:00:00Z",
        "title": f"event {n}",
    }
    if audience is not None:
        base["audience"] = audience
    base.update(over)
    return base


def seed(client):
    r = client.post(
        "/events",
        json=[
            ev(1, ["self"], title="SECRET self-only one"),
            ev(2, ["self", "family"], title="shared two"),
            ev(3, ["family"], title="family three"),
            ev(4, ["self"], title="SECRET self-only four"),
        ],
    )
    assert r.status_code == 201


# --- token store -------------------------------------------------------------


def test_create_and_verify(conn):
    issued = create_token(conn, ["family", "ops"], now="2026-11-06T12:00:00Z")
    p = verify_token(conn, issued.value, "2026-11-06T12:00:00Z")
    assert p.token_id == issued.id
    assert p.labels == frozenset({"family", "ops"})


def test_only_a_hash_is_stored(conn):
    issued = create_token(conn, ["family"])
    secret = issued.value.split(".", 1)[1]
    row = conn.execute("SELECT * FROM tokens").fetchone()
    assert secret not in "".join(str(v) for v in tuple(row))
    assert row["secret_hash"] != secret and len(row["secret_hash"]) == 64


def test_wrong_secret_unknown_id_and_garbage_are_rejected(conn):
    issued = create_token(conn, ["family"])
    now = "2026-11-06T12:00:00Z"
    assert verify_token(conn, issued.id + ".nope", now) is None
    assert verify_token(conn, "ffffffffffff." + issued.value.split(".", 1)[1], now) is None
    for junk in ["", "x", ".", "a.b.c", "\x00"]:
        assert verify_token(conn, junk, now) is None


def test_expiry_is_enforced(conn):
    issued = create_token(conn, ["family"], expires_at="2026-11-07T00:00:00Z")
    assert verify_token(conn, issued.value, "2026-11-06T23:59:59Z")
    assert verify_token(conn, issued.value, "2026-11-07T00:00:00Z") is None


def test_revocation_is_enforced(conn):
    issued = create_token(conn, ["family"])
    assert revoke_token(conn, issued.id, "2026-11-06T12:00:00Z")
    assert verify_token(conn, issued.value, "2026-11-06T12:00:01Z") is None
    assert not revoke_token(conn, "doesnotexist", "2026-11-06T12:00:00Z")


@pytest.mark.parametrize("bad", [[], ["Family"], ["a b"], [""], ["x" * 40]])
def test_invalid_labels_rejected(conn, bad):
    with pytest.raises(ValueError):
        create_token(conn, bad)


def test_list_tokens_never_exposes_secrets(conn):
    issued = create_token(conn, ["family"], note="grandma")
    [row] = list_tokens(conn)
    assert row["id"] == issued.id and row["labels"] == ["family"] and row["note"] == "grandma"
    assert issued.value.split(".", 1)[1] not in str(row)


# --- deny by default ---------------------------------------------------------


@pytest.mark.parametrize("path", ["/feed.json", "/feed.atom"])
def test_no_credential_is_401(db_path, path):
    client = TestClient(create_app(db_path))
    r = client.get(path)
    assert r.status_code == 401
    assert "Bearer" in r.headers["www-authenticate"]
    assert "no-store" in r.headers["cache-control"]


def test_garbage_expired_and_revoked_tokens_are_401(db_path, conn, clock):
    app = create_app(db_path, clock=clock)
    client = TestClient(app)
    expired = create_token(conn, ["self"], expires_at="2026-11-06T11:00:00Z")
    revoked = create_token(conn, ["self"])
    revoke_token(conn, revoked.id, "2026-11-06T11:00:00Z")
    for value in ["garbage", expired.value, revoked.value]:
        assert (
            client.get("/feed.json", headers={"Authorization": f"Bearer {value}"}).status_code
            == 401
        )
        assert client.get(f"/t/{value}/feed.json").status_code == 401


def test_header_and_path_credentials_both_work(db_path, conn):
    client = TestClient(create_app(db_path))
    issued = create_token(conn, ["self"])
    assert (
        client.get("/feed.json", headers={"Authorization": f"Bearer {issued.value}"}).status_code
        == 200
    )
    assert client.get(f"/t/{issued.value}/feed.json").status_code == 200
    assert client.get(f"/t/{issued.value}/feed.atom").status_code == 200


def test_query_string_cannot_supply_labels_or_credentials(db_path, conn):
    client = authed_client(db_path)
    issued = create_token(conn, ["family"])
    seed(client)
    h = {"Authorization": f"Bearer {issued.value}"}
    assert client.get("/feed.json?audience=self", headers=h).status_code == 400
    anon = TestClient(create_app(db_path))
    assert anon.get(f"/feed.json?token={issued.value}").status_code == 401


# --- audience filtering ------------------------------------------------------


def titles(r):
    return {i["title"] for i in r.json()["items"]}


def test_family_token_never_sees_self_only_events(db_path):
    owner = authed_client(db_path, labels=("self",))
    seed(owner)
    family = authed_client(db_path, labels=("family",))
    r = family.get("/feed.json")
    assert titles(r) == {"shared two", "family three"}
    assert "SECRET" not in r.text
    assert titles(owner.get("/feed.json")) == {
        "SECRET self-only one",
        "shared two",
        "SECRET self-only four",
    }


def test_atom_is_filtered_too(db_path):
    owner = authed_client(db_path)
    seed(owner)
    family = authed_client(db_path, labels=("family",))
    r = family.get("/feed.atom")
    assert "SECRET" not in r.text
    assert len(ET.fromstring(r.content).findall(f"{ATOM}entry")) == 2


def test_cursors_and_paging_never_reveal_hidden_positions(db_path):
    owner = authed_client(db_path)
    seed(owner)  # seqs: 1 self, 2 shared, 3 family, 4 self
    family = authed_client(db_path, labels=("family",))
    tail = family.get("/feed.json").json()
    assert tail["_dayfile"]["cursor"] == 3  # not 4
    page = family.get("/feed.json?since_seq=0&limit=1").json()
    assert page["_dayfile"]["cursor"] == 2 and "since_seq=2" in page["next_url"]
    nxt = family.get(page["next_url"]).json()
    assert nxt["_dayfile"]["cursor"] == 3 and "next_url" not in nxt
    empty = family.get("/feed.json?since_seq=3").json()
    assert empty["items"] == [] and empty["_dayfile"]["cursor"] == 3


def test_filters_cannot_be_used_to_probe_hidden_events(db_path):
    owner = authed_client(db_path)
    seed(owner)
    family = authed_client(db_path, labels=("family",))
    r = family.get("/feed.json?tag=note&tag_any=x&status=retracted&status=open")
    assert r.status_code == 200 and "SECRET" not in r.text


def test_lifecycle_from_hidden_events_does_not_leak_titles(db_path):
    owner = authed_client(db_path)
    owner.post("/events", json=ev(1, ["family"], source={"id": "a", "ref": "x"}))
    owner.post(
        "/events",
        json=ev(
            2,
            ["self", "family"],
            source={"id": "a", "ref": "y"},
            title="SECRET completion note",
            refs=[{"rel": "completes", "id": "01JC8Z4Q7M2X9K3T5W6N8R0001"}],
        ),
    )
    family = authed_client(db_path, labels=("family",))
    assert "SECRET" in family.get("/feed.json").text  # shared with family: fine
    only_self = ev(5, ["self"], title="SECRET hidden", source={"id": "a", "ref": "z"})
    owner.post("/events", json=only_self)
    assert "hidden" not in family.get("/feed.json").text


# --- caching -----------------------------------------------------------------


def test_private_cache_headers_on_200_and_304(db_path):
    client = authed_client(db_path)
    seed(client)
    first = client.get("/feed.json")
    assert "private" in first.headers["cache-control"]
    assert first.headers["vary"] == "Authorization"
    again = client.get("/feed.json", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert "private" in again.headers["cache-control"] and again.headers["vary"] == "Authorization"


def test_etags_differ_across_labels_and_identities(db_path):
    owner = authed_client(db_path, labels=("self", "family"))
    seed(owner)
    family = authed_client(db_path, labels=("family",))
    twin = authed_client(db_path, labels=("family",))
    a, b, c = (x.get("/feed.json").headers["etag"] for x in (owner, family, twin))
    assert len({a, b, c}) == 3  # different labels and different holders never share a validator


def test_conditional_get_works_with_path_credentials(db_path, conn):
    client = TestClient(create_app(db_path))
    issued = create_token(conn, ["self"])
    url = f"/t/{issued.value}/feed.json"
    first = client.get(url)
    assert client.get(url, headers={"If-None-Match": first.headers["etag"]}).status_code == 304


def test_a_cached_etag_does_not_bypass_revocation(db_path, conn, clock):
    client = TestClient(create_app(db_path, clock=clock))
    issued = create_token(conn, ["self"])
    h = {"Authorization": f"Bearer {issued.value}"}
    etag = client.get("/feed.json", headers=h).headers["etag"]
    revoke_token(conn, issued.id, clock())
    assert client.get("/feed.json", headers={**h, "If-None-Match": etag}).status_code == 401


# --- never log secrets -------------------------------------------------------


def test_redaction_filter_rewrites_token_paths():
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/t/abc123.SECRETVALUE/feed.json?x=1", "1.1", 200),
        None,
    )  # fmt: skip
    assert RedactTokens().filter(rec)
    out = rec.getMessage()
    assert "SECRETVALUE" not in out and "/t/[redacted]/feed.json?x=1" in out


def test_app_logs_contain_no_token_values(db_path, conn, caplog):
    client = TestClient(create_app(db_path))
    issued = create_token(conn, ["self"], expires_at="2020-01-01T00:00:00Z")
    secret = issued.value.split(".", 1)[1]
    with caplog.at_level(logging.DEBUG, logger="dayfile"):  # httpx logs its own client-side URLs
        client.get(f"/t/{issued.value}/feed.json")
        client.get("/feed.json", headers={"Authorization": f"Bearer {issued.id}.{secret}x"})
        client.get("/feed.json", headers={"Authorization": "Bearer totally-made-up"})
    assert secret not in caplog.text and "totally-made-up" not in caplog.text
    assert issued.id in caplog.text  # ids are fine to log


def test_real_server_access_log_never_prints_the_token(db_path, conn, tmp_path):
    issued = create_token(conn, ["self"])
    secret = issued.value.split(".", 1)[1]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "dayfile.app:app", "--port", str(port)],
        env={"DAYFILE_DB": str(db_path), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        url = f"http://127.0.0.1:{port}/t/{issued.value}/feed.json"
        for _ in range(100):
            try:
                assert urllib.request.urlopen(url, timeout=2).status == 200
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("server did not start")
    finally:
        proc.terminate()
        output = proc.communicate(timeout=10)[0]
    assert "/t/[redacted]/feed.json" in output
    assert secret not in output


# --- discovery (M8) ----------------------------------------------------------


def test_401_carries_a_feed_challenge_besides_bearer(db_path):
    r = TestClient(create_app(db_path)).get("/feed.json")
    challenges = r.headers.get_list("www-authenticate")
    assert any(c.startswith("Bearer ") for c in challenges)
    feed = next(c for c in challenges if c.startswith("Feed "))
    assert 'realm="dayfile"' in feed and 'path="/t/{token}/"' in feed
    assert "authorization_uri" not in feed


def test_authorization_uri_is_advertised_when_configured(db_path, conn):
    client = TestClient(create_app(db_path, auth_uri="https://dayfile.example/get-access"))
    challenge = next(
        c
        for c in client.get("/feed.json").headers.get_list("www-authenticate")
        if c.startswith("Feed ")
    )
    assert 'authorization_uri="https://dayfile.example/get-access"' in challenge
    issued = create_token(conn, ["self"])
    ok = client.get("/feed.json", headers={"Authorization": f"Bearer {issued.value}"})
    assert '<https://dayfile.example/get-access>; rel="authorization"' in ok.headers["link"]
    assert ok.json()["_dayfile"]["authorization_uri"] == "https://dayfile.example/get-access"
    atom = client.get("/feed.atom", headers={"Authorization": f"Bearer {issued.value}"})
    links = ET.fromstring(atom.content).findall(f"{ATOM}link")
    assert any(lk.get("rel") == "authorization" for lk in links)


def test_no_authorization_link_without_configuration(db_path, conn):
    client = TestClient(create_app(db_path))
    issued = create_token(conn, ["self"])
    ok = client.get("/feed.json", headers={"Authorization": f"Bearer {issued.value}"})
    assert "authorization" not in ok.headers.get("link", "")
