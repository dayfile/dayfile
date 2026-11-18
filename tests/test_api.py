"""Native JSON API: GET /events, GET /events/{id}, long-poll and the SSE stream."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from dayfile.app import create_app
from dayfile.auth.tokens import create_token
from dayfile.db import connect
from dayfile.ingest.core import ingest_events
from helpers import authed_client


class Clock:
    def __init__(self, now="2026-11-06T12:00:00Z"):
        self.now = now

    def __call__(self):
        return self.now


def eid(n):
    return f"01JC8Z4Q7M2X9K3T5W6N8R{n:04d}"


def ev(n, **over):
    base = {
        "specversion": "dayfile/1",
        "id": eid(n),
        "source": {"id": "t", "ref": f"r{n}"},
        "occurred_at": "2026-11-06T10:00:00Z",
        "title": f"e{n}",
    }
    return {**base, **over}


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "d.db"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(db_path, clock):
    return authed_client(db_path, clock=clock, labels=("self", "family"))


def titles(r):
    return [e["title"] for e in r.json()["events"]]


# --- GET /events ---------------------------------------------------------------


def test_events_returns_native_envelopes_oldest_first(client):
    client.post("/events", json=[ev(1, tags=["a"]), ev(2, urgency=2, payload={"k": 1})])
    r = client.get("/events")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert titles(r) == ["e1", "e2"]
    first = body["events"][0]
    assert first["source"] == {"id": "t", "ref": "r1"}  # fields the feed formats drop
    assert first["occurred_at"] == "2026-11-06T10:00:00Z" and first["audience"] == ["self"]
    assert (
        first["seq"] == 1 and "status" not in first and first["tags"] == ["a"]
    )  # a note has no status
    assert body["events"][1]["payload"] == {"k": 1}
    assert body["cursor"] == 2 and body["released_cursor"] == "2026-11-06T12:00:00Z"
    assert body["next"] is None


def test_events_filters_and_pages_like_the_feeds(client):
    client.post("/events", json=[ev(n, tags=["x"] if n % 2 else []) for n in range(1, 8)])
    assert titles(client.get("/events?tag=x")) == ["e1", "e3", "e5", "e7"]
    page = client.get("/events?since_seq=0&limit=3").json()
    assert [e["title"] for e in page["events"]] == ["e1", "e2", "e3"]
    assert "since_seq=3" in page["next"] and "released_after=" in page["next"]
    rest = client.get(page["next"]).json()
    assert [e["title"] for e in rest["events"]] == ["e4", "e5", "e6"]


def test_events_requires_a_credential_and_rejects_bad_queries(client, db_path):
    anon = TestClient(create_app(db_path))
    assert anon.get("/events").status_code == 401
    assert anon.get(f"/events?token={client.read_auth}").status_code == 401
    assert client.get("/events?audience=self").status_code == 400
    assert client.get("/events?wait=5").status_code == 400  # wait needs since_seq
    assert client.get("/events?since_seq=0&wait=999").status_code == 400
    assert client.get("/events?since_seq=0&wait=abc").status_code == 400


def test_path_tokens_do_not_work_on_the_api(client):
    assert client.get(f"/t/{client.read_auth}/events").status_code == 404


def test_audience_is_filtered_and_nothing_leaks(db_path, clock):
    owner = authed_client(db_path, clock=clock, labels=("self",))
    owner.post("/events", json=[ev(1, audience=["self"]), ev(2, audience=["family"])])
    conn = connect(db_path)
    fam = create_token(conn, ["family"])
    r = owner.get("/events", headers={"Authorization": f"Bearer {fam.value}"})
    assert titles(r) == ["e2"] and r.json()["cursor"] == 2
    assert "e1" not in r.text


def test_write_tokens_cannot_read(client, db_path):
    w = create_token(connect(db_path), ["self"], write_sources=["t"])
    assert client.get("/events", headers={"Authorization": f"Bearer {w.value}"}).status_code == 403


def test_caching_headers_and_per_caller_etag(db_path, clock):
    a = authed_client(db_path, clock=clock, labels=("self",))
    a.post("/events", json=ev(1))
    r = a.get("/events")
    assert "private" in r.headers["cache-control"] and r.headers["vary"] == "Authorization"
    etag = r.headers["etag"]
    assert a.get("/events", headers={"If-None-Match": etag}).status_code == 304
    other = create_token(connect(db_path), ["self"])
    r2 = a.get("/events", headers={"Authorization": f"Bearer {other.value}"})
    assert r2.headers["etag"] != etag


def test_time_release_and_default_status_apply(client, clock):
    client.post(
        "/events",
        json=[
            ev(1, visible_at="2026-11-06T13:00:00Z"),
            ev(2),
            ev(3, expires_at="2026-11-06T11:00:00Z"),
        ],
    )
    assert titles(client.get("/events")) == ["e2"]
    clock.now = "2026-11-06T13:00:00Z"
    assert titles(client.get("/events")) == ["e1", "e2"]


def test_late_release_arrives_through_the_cursors(client, clock):
    client.post("/events", json=[ev(1, visible_at="2026-11-06T13:00:00Z"), ev(2)])
    first = client.get("/events?since_seq=0").json()
    clock.now = "2026-11-06T13:30:00Z"
    again = client.get(
        f"/events?since_seq={first['cursor']}&released_after={first['released_cursor']}"
    )
    assert titles(again) == ["e1"]
    assert again.json()["cursor"] == 2  # never moves backwards


# --- GET /events/{id} -----------------------------------------------------------


def test_get_one_event_includes_hidden_by_default_states(client):
    client.post(
        "/events",
        json=[ev(1), ev(2, refs=[{"rel": "retracts", "id": eid(1)}])],
    )
    r = client.get(f"/events/{eid(1)}")
    assert r.status_code == 200 and r.json()["status"] == "retracted"  # not hidden by id
    assert r.json()["title"] == "e1"


def test_unknown_unreleased_expired_and_unauthorized_are_all_404(db_path, clock):
    owner = authed_client(db_path, clock=clock, labels=("self",))
    owner.post(
        "/events",
        json=[
            ev(1, audience=["family"]),
            ev(2, visible_at="2026-11-06T13:00:00Z"),
            ev(3, expires_at="2026-11-06T11:00:00Z"),
        ],
    )
    for n in (1, 2, 3, 99):
        r = owner.get(f"/events/{eid(n)}")
        assert r.status_code == 404 and r.json() == {"error": "not found"}


def test_get_one_requires_credentials_and_has_etag(client, db_path):
    client.post("/events", json=ev(1))
    assert TestClient(create_app(db_path)).get(f"/events/{eid(1)}").status_code == 401
    r = client.get(f"/events/{eid(1)}")
    assert (
        client.get(f"/events/{eid(1)}", headers={"If-None-Match": r.headers["etag"]}).status_code
        == 304
    )


def test_malformed_id_is_404_not_500(client):
    assert client.get("/events/%00%00").status_code == 404
    assert client.get("/events/" + "x" * 5000).status_code == 404


# --- long-poll ------------------------------------------------------------------


@pytest.fixture
def fast_client(db_path, clock):
    app = create_app(db_path, clock=clock, poll_interval=0.02, stream_seconds=0.4)
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {create_token(connect(db_path), ['self']).value}"
    return c


def test_wait_times_out_with_an_empty_page(fast_client):
    started = time.monotonic()
    r = fast_client.get("/events?since_seq=0&wait=1")
    assert r.status_code == 200 and r.json()["events"] == []
    assert 0.8 <= time.monotonic() - started < 3


def test_wait_wakes_up_when_an_event_arrives(fast_client, db_path):
    def later():
        time.sleep(0.3)
        c = connect(db_path)
        ingest_events(c, [ev(1)])
        c.close()

    t = threading.Thread(target=later)
    t.start()
    started = time.monotonic()
    r = fast_client.get("/events?since_seq=0&wait=5")
    t.join()
    assert titles(r) == ["e1"] and time.monotonic() - started < 3


def test_wait_returns_at_once_if_data_is_already_there(fast_client, db_path):
    c = connect(db_path)
    ingest_events(c, [ev(1)])
    started = time.monotonic()
    assert titles(fast_client.get("/events?since_seq=0&wait=5")) == ["e1"]
    assert time.monotonic() - started < 1


# --- SSE ------------------------------------------------------------------------


def parse_sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            k, _, v = line.partition(": ")
            fields[k] = v
        if "data" in fields:
            out.append(fields)
    return out


def test_stream_replays_from_since_seq_and_ends_after_its_lifetime(fast_client, db_path):
    c = connect(db_path)
    ingest_events(c, [ev(1), ev(2)])
    r = fast_client.get("/events/stream?since_seq=0")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "no-cache" in r.headers["cache-control"] and "private" in r.headers["cache-control"]
    got = parse_sse(r.text)
    assert [json.loads(g["data"])["title"] for g in got] == ["e1", "e2"]
    assert got[0]["event"] == "event" and got[-1]["id"].startswith("2:")


def test_stream_resumes_from_last_event_id_without_duplicates(fast_client, db_path):
    c = connect(db_path)
    ingest_events(c, [ev(1), ev(2), ev(3)])
    first = parse_sse(fast_client.get("/events/stream?since_seq=0").text)
    resume = first[0]["id"]  # after e1
    r = fast_client.get("/events/stream", headers={"Last-Event-ID": resume})
    assert [json.loads(g["data"])["title"] for g in parse_sse(r.text)] == ["e2", "e3"]


def test_stream_without_a_cursor_starts_at_now(fast_client, db_path):
    c = connect(db_path)
    ingest_events(c, [ev(1)])
    assert parse_sse(fast_client.get("/events/stream").text) == []  # no history replay


def test_stream_delivers_events_that_arrive_while_open(fast_client, db_path):
    def later():
        time.sleep(0.15)
        c = connect(db_path)
        ingest_events(c, [ev(5)])
        c.close()

    t = threading.Thread(target=later)
    t.start()
    got = parse_sse(fast_client.get("/events/stream").text)
    t.join()
    assert [json.loads(g["data"])["title"] for g in got] == ["e5"]


def test_stream_requires_a_credential_and_filters_audience(db_path, clock):
    app = create_app(db_path, clock=clock, poll_interval=0.02, stream_seconds=0.3)
    anon = TestClient(app)
    assert anon.get("/events/stream").status_code == 401
    conn = connect(db_path)
    ingest_events(conn, [ev(1, audience=["self"]), ev(2, audience=["family"])])
    fam = create_token(conn, ["family"])
    r = anon.get("/events/stream?since_seq=0", headers={"Authorization": f"Bearer {fam.value}"})
    assert [json.loads(g["data"])["title"] for g in parse_sse(r.text)] == ["e2"]
    assert "e1" not in r.text


def test_stream_ends_when_the_token_is_revoked(db_path, clock):
    from dayfile.auth.tokens import revoke_token

    app = create_app(db_path, clock=clock, poll_interval=0.02, stream_seconds=3)
    conn = connect(db_path)
    tok = create_token(conn, ["self"])
    threading.Timer(
        0.2, lambda: revoke_token(connect(db_path), tok.id, "2026-11-06T12:00:00Z")
    ).start()
    started = time.monotonic()
    r = TestClient(app).get("/events/stream", headers={"Authorization": f"Bearer {tok.value}"})
    assert r.status_code == 200 and time.monotonic() - started < 2.5
