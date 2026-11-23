"""Write credentials: who may POST /events, as which source, with which audience."""

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.auth.tokens import create_token, list_tokens, verify_token
from dayfile.db import connect


def ev(n, source="adapter:phone", audience=None, **over):
    base = {
        "specversion": "dayfile/1",
        "id": f"01JC8Z4Q7M2X9K3T5W6N8R{n:04d}"[:26],
        "source": {"id": source, "ref": f"r{n}"},
        "occurred_at": "2026-11-06T10:00:00Z",
        "title": f"event {n}",
    }
    if audience is not None:
        base["audience"] = audience
    return {**base, **over}


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "d.db"


@pytest.fixture
def conn(db_path):
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def client(db_path):
    return TestClient(create_app(db_path))


def bearer(token):
    return {"Authorization": f"Bearer {token.value}"}


def stored(conn):
    return conn.execute("SELECT count(*) FROM events").fetchone()[0]


def test_post_without_credential_is_401_and_writes_nothing(client, conn):
    r = client.post("/events", json=ev(1))
    assert r.status_code == 401 and "Bearer" in r.headers["www-authenticate"]
    assert stored(conn) == 0


def test_garbage_credential_is_401(client, conn):
    assert (
        client.post("/events", json=ev(1), headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )
    assert stored(conn) == 0


def test_path_and_query_credentials_do_not_authorize_writes(client, conn):
    w = create_token(conn, ["self"], write_sources=["adapter:phone"])
    assert client.post(f"/events?token={w.value}", json=ev(1)).status_code == 401
    assert client.post(f"/t/{w.value}/events", json=ev(1)).status_code in (404, 405)
    assert stored(conn) == 0


def test_read_token_cannot_write(client, conn):
    r = client.post("/events", json=ev(1), headers=bearer(create_token(conn, ["self"])))
    assert r.status_code == 403 and "read-only" in r.json()["error"]
    assert stored(conn) == 0


def test_write_token_cannot_read(client, conn):
    w = create_token(conn, ["self"], write_sources=["adapter:phone"])
    r = client.get("/feed.json", headers=bearer(w))
    assert r.status_code == 403
    assert client.get(f"/t/{w.value}/feed.json").status_code == 403


def test_writer_can_post_as_its_source(client, conn):
    w = create_token(conn, ["self", "family"], write_sources=["adapter:phone"])
    r = client.post("/events", json=ev(1, audience=["family"]), headers=bearer(w))
    assert r.status_code == 201 and stored(conn) == 1


def test_writer_cannot_post_as_another_source(client, conn):
    w = create_token(conn, ["self"], write_sources=["adapter:phone"])
    r = client.post("/events", json=ev(1, source="adapter:email"), headers=bearer(w))
    assert r.status_code == 403 and stored(conn) == 0


def test_writer_cannot_grant_labels_it_lacks(client, conn):
    w = create_token(conn, ["self"], write_sources=["adapter:phone"])
    r = client.post("/events", json=ev(1, audience=["self", "family"]), headers=bearer(w))
    assert r.status_code == 403 and stored(conn) == 0


def test_default_audience_is_checked_too(client, conn):
    """An event with no audience is 'self'; a writer without 'self' cannot create it."""
    w = create_token(conn, ["family"], write_sources=["adapter:phone"])
    assert client.post("/events", json=ev(1), headers=bearer(w)).status_code == 403
    assert client.post("/events", json=ev(2, audience=[]), headers=bearer(w)).status_code == 403
    assert stored(conn) == 0


def test_one_bad_event_rejects_the_whole_batch(client, conn):
    w = create_token(conn, ["self"], write_sources=["adapter:phone"])
    batch = [ev(1), ev(2, source="adapter:email")]
    assert client.post("/events", json=batch, headers=bearer(w)).status_code == 403
    assert stored(conn) == 0


def test_forbidden_does_not_reveal_whether_event_exists(client, conn):
    """A redelivery as another source gets 403 whether or not the original exists."""
    owner = create_token(conn, ["self"], write_sources=["adapter:phone"])
    client.post("/events", json=ev(1), headers=bearer(owner))
    other = create_token(conn, ["self"], write_sources=["adapter:other"])
    r = client.post("/events", json=ev(1), headers=bearer(other))
    assert r.status_code == 403
    assert "01JC8Z4Q7M2X9K3T5W6N8R0001" not in r.text


def test_multiple_sources_per_token(client, conn):
    w = create_token(conn, ["self"], write_sources=["a:one", "a:two"])
    r = client.post(
        "/events", json=[ev(1, source="a:one"), ev(2, source="a:two")], headers=bearer(w)
    )
    assert r.status_code == 201
    assert verify_token(conn, w.value, "2026-11-06T00:00:00Z").writer_sources == {"a:one", "a:two"}


def test_empty_write_sources_rejected(conn):
    for bad in ([], [""], ["  "]):
        with pytest.raises(ValueError):
            create_token(conn, ["self"], write_sources=bad)


def test_revoked_and_expired_writers_are_401(client, conn):
    from dayfile.auth.tokens import revoke_token

    w = create_token(conn, ["self"], write_sources=["adapter:phone"])
    revoke_token(conn, w.id, "2026-11-06T00:00:00Z")
    assert client.post("/events", json=ev(1), headers=bearer(w)).status_code == 401
    old = create_token(
        conn, ["self"], write_sources=["adapter:phone"], expires_at="2000-01-01T00:00:00Z"
    )
    assert client.post("/events", json=ev(2), headers=bearer(old)).status_code == 401


def test_token_list_shows_write_sources_not_secrets(conn):
    w = create_token(conn, ["self"], write_sources=["adapter:phone"])
    listed = [t for t in list_tokens(conn) if t["id"] == w.id][0]
    assert listed["write_sources"] == ["adapter:phone"]
    assert w.value.partition(".")[2] not in str(listed)


def test_lifecycle_cannot_be_forged_across_audiences(client, conn):
    """A family-only writer retracting a self-only event has no effect (no audience overlap)."""
    owner = create_token(conn, ["self"], write_sources=["adapter:phone"])
    forger = create_token(conn, ["family"], write_sources=["adapter:evil"])
    reader = create_token(conn, ["self"])
    client.post("/events", json=ev(1, tags=["todo"]), headers=bearer(owner))
    forged = ev(
        2,
        source="adapter:evil",
        audience=["family"],
        refs=[{"rel": "retracts", "id": ev(1)["id"]}],
    )
    assert client.post("/events", json=forged, headers=bearer(forger)).status_code == 201
    r = client.get("/feed.json?status=open", headers=bearer(reader))
    assert [i["title"] for i in r.json()["items"]] == ["event 1"]


def test_cli_creates_write_token(db_path, capsys):
    from dayfile.cli import main

    assert (
        main(
            [
                "token",
                "create",
                "--labels",
                "self",
                "--write-source",
                "a:one,a:two",
                "--db",
                str(db_path),
            ]
        )
        == 0
    )
    out = capsys.readouterr()
    assert "/feed.json" not in out.out  # a write token has no feed URL
    token = out.out.splitlines()[0]
    c = connect(db_path)
    assert verify_token(c, token, "2026-11-06T00:00:00Z").writer_sources == {"a:one", "a:two"}


def test_old_database_gains_token_write_on_open(db_path):
    c = connect(db_path)
    c.executescript("DROP TABLE token_write; PRAGMA user_version = 4;")
    c.close()
    c = connect(db_path)
    assert create_token(c, ["self"], write_sources=["a:one"]).id
    assert c.execute("PRAGMA user_version").fetchone()[0] >= 5


def test_write_rate_limit_counts_events_per_token(db_path, conn):
    clock = [1000.0]
    app = create_app(db_path, write_limit=(3, 60), monotonic=lambda: clock[0])
    client = TestClient(app)
    a = create_token(conn, ["self"], write_sources=["adapter:phone"])
    b = create_token(conn, ["self"], write_sources=["adapter:phone"])
    assert client.post("/events", json=[ev(1), ev(2)], headers=bearer(a)).status_code == 201
    over = client.post("/events", json=[ev(3), ev(4)], headers=bearer(a))
    assert over.status_code == 429 and int(over.headers["retry-after"]) >= 1
    assert stored(conn) == 2  # the rejected batch wrote nothing
    assert client.post("/events", json=ev(5), headers=bearer(b)).status_code == 201  # other token
    clock[0] += 61  # window passes
    assert client.post("/events", json=[ev(3), ev(4)], headers=bearer(a)).status_code == 201


def test_rate_limit_does_not_apply_to_failed_authentication(db_path, conn):
    client = TestClient(create_app(db_path, write_limit=(1, 60)))
    for _ in range(3):
        assert (
            client.post("/events", json=ev(1), headers={"Authorization": "Bearer x.y"}).status_code
            == 401
        )


def test_a_title_alone_is_a_valid_post_with_a_single_source_token(client, conn):
    t = create_token(conn, ["self"], write_sources=["adapter:phone"])
    r = client.post("/events", json={"title": "Dentist moved"}, headers=bearer(t))
    assert r.status_code == 201
    e = r.json()
    assert (e["specversion"], e["source"]) == ("dayfile/1", {"id": "adapter:phone"})
    assert "type" not in e  # no type unless the caller gives one
    assert len(e["id"]) == 26 and e["occurred_at"].endswith("Z")


def test_each_quick_post_is_a_new_event(client, conn):
    t = create_token(conn, ["self"], write_sources=["adapter:phone"])
    for _ in range(2):
        assert client.post("/events", json={"title": "same"}, headers=bearer(t)).status_code == 201
    assert stored(conn) == 2


def test_quick_post_values_the_caller_gave_win(client, conn):
    t = create_token(conn, ["self"], write_sources=["adapter:phone"])
    r = client.post("/events", json={"title": "x", "tags": ["todo", "a"]}, headers=bearer(t))
    assert r.json()["tags"] == ["todo", "a"]


def test_quick_post_needs_a_source_when_the_token_has_several(client, conn):
    t = create_token(conn, ["self"], write_sources=["a", "b"])
    assert client.post("/events", json={"title": "x"}, headers=bearer(t)).status_code == 422
    r = client.post("/events", json={"title": "x", "source": {"id": "b"}}, headers=bearer(t))
    assert r.status_code == 201


def test_quick_post_still_requires_a_title_and_works_in_arrays(client, conn):
    t = create_token(conn, ["self"], write_sources=["adapter:phone"])
    assert client.post("/events", json={}, headers=bearer(t)).status_code == 422
    r = client.post("/events", json=[{"title": "a"}, {"title": "b"}], headers=bearer(t))
    assert r.status_code == 201 and len(r.json()) == 2


def test_quick_post_does_not_complete_signed_events(client, conn):
    t = create_token(conn, ["self"], write_sources=["adapter:phone"])
    r = client.post("/events", json={"title": "x", "sig": "AAAA"}, headers=bearer(t))
    assert r.status_code == 422


def _open_todo(client, t):
    return client.post(
        "/events", json={"title": "Renew domain", "tags": ["todo"]}, headers=bearer(t)
    ).json()


def test_close_endpoint_completes_a_todo_with_a_reason(client, conn):
    t = create_token(conn, ["self"], write_sources=["adapter:phone"])
    todo = _open_todo(client, t)
    r = client.post(
        f"/events/{todo['id']}/close", json={"body": "Renewed through 2027."}, headers=bearer(t)
    )
    assert r.status_code == 201
    closing = r.json()
    assert closing["refs"] == [{"rel": "completes", "id": todo["id"]}]
    assert closing["body"] == "Renewed through 2027." and closing["title"] == "Renew domain"
    read = create_token(conn, ["self"])
    events = client.get("/events", headers=bearer(read)).json()["events"]
    assert next(e for e in events if e["id"] == todo["id"])["status"] == "closed"


def test_close_endpoint_takes_no_body_or_an_own_title(client, conn):
    t = create_token(conn, ["self"], write_sources=["adapter:phone"])
    a, b = _open_todo(client, t), _open_todo(client, t)
    assert client.post(f"/events/{a['id']}/close", headers=bearer(t)).status_code == 201
    r = client.post(f"/events/{b['id']}/close", json={"title": "Done"}, headers=bearer(t))
    assert r.status_code == 201 and r.json()["title"] == "Done"


def test_close_endpoint_refuses_anything_but_a_title_and_a_body(client, conn):
    t = create_token(conn, ["self"], write_sources=["adapter:phone"])
    todo = _open_todo(client, t)
    for body in ({"tags": ["x"]}, {"refs": []}, ["a"], "text"):
        r = client.post(f"/events/{todo['id']}/close", json=body, headers=bearer(t))
        assert r.status_code == 422
    assert client.post("/events/not-an-id/close", headers=bearer(t)).status_code == 422


def test_close_endpoint_needs_a_write_token_and_a_real_target(client, conn):
    t = create_token(conn, ["self"], write_sources=["adapter:phone"])
    todo = _open_todo(client, t)
    assert client.post(f"/events/{todo['id']}/close").status_code == 401
    read = create_token(conn, ["self"])
    assert client.post(f"/events/{todo['id']}/close", headers=bearer(read)).status_code == 403
    missing = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert client.post(f"/events/{missing}/close", headers=bearer(t)).status_code == 422


def test_retract_endpoint_hides_an_event_and_undoing_a_close_reopens_it(client, conn):
    t = create_token(conn, ["self"], write_sources=["adapter:phone"])
    read = create_token(conn, ["self"])
    todo = _open_todo(client, t)
    closing = client.post(f"/events/{todo['id']}/close", headers=bearer(t)).json()
    r = client.post(
        f"/events/{closing['id']}/retract", json={"body": "closed by mistake"}, headers=bearer(t)
    )
    assert r.status_code == 201
    assert r.json()["refs"] == [{"rel": "retracts", "id": closing["id"]}]
    assert r.json()["body"] == "closed by mistake"
    status = {
        e["id"]: e.get("status")
        for e in client.get("/events", headers=bearer(read)).json()["events"]
    }
    assert status[todo["id"]] == "open"  # retracting the close reopened it
    client.post(f"/events/{todo['id']}/retract", headers=bearer(t))
    url = "/events?status=retracted"
    assert todo["id"] in [e["id"] for e in client.get(url, headers=bearer(read)).json()["events"]]


def test_retract_endpoint_has_the_same_limits_as_close(client, conn):
    t = create_token(conn, ["self"], write_sources=["adapter:phone"])
    todo = _open_todo(client, t)
    assert client.post(f"/events/{todo['id']}/retract").status_code == 401
    assert (
        client.post(
            f"/events/{todo['id']}/retract", json={"tags": ["x"]}, headers=bearer(t)
        ).status_code
        == 422
    )
    assert client.post("/events/nope/retract", headers=bearer(t)).status_code == 422
