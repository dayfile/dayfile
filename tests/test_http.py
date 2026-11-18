import json
from pathlib import Path

import pytest

from dayfile.db import connect
from helpers import authed_client

EXAMPLES = [
    json.loads(line)
    for line in (Path(__file__).parents[1] / "examples" / "events.jsonl").read_text().splitlines()
    if line.strip()
]


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "d.db"


@pytest.fixture
def client(db_path):
    return authed_client(db_path)


def rows(db_path):
    c = connect(db_path)
    try:
        return c.execute("SELECT count(*) FROM events").fetchone()[0]
    finally:
        c.close()


def test_post_single_event(client):
    r = client.post("/events", json=EXAMPLES[3])
    assert r.status_code == 201
    body = r.json()
    assert body["seq"] == 1
    assert body["audience"] == ["self"]
    assert body["visible_at"] == body["occurred_at"]


def test_post_duplicate_returns_existing_with_200(client):
    first = client.post("/events", json=EXAMPLES[3]).json()
    r = client.post("/events", json=EXAMPLES[3])
    assert r.status_code == 200
    assert r.json() == first


def test_post_array_returns_list(client):
    r = client.post("/events", json=EXAMPLES)
    assert r.status_code == 201
    assert [e["seq"] for e in r.json()] == list(range(1, 9))


def test_examples_twice_yield_8_rows(client, db_path):
    client.post("/events", json=EXAMPLES)
    r = client.post("/events", json=EXAMPLES)
    assert r.status_code == 200
    assert rows(db_path) == 8


def test_malformed_event_returns_422_and_writes_nothing(client, db_path):
    bad = dict(EXAMPLES[3])
    del bad["title"]
    r = client.post("/events", json=[EXAMPLES[0], bad])
    assert r.status_code == 422
    assert "title" in json.dumps(r.json())
    assert rows(db_path) == 0


def test_invalid_json_returns_422(client):
    r = client.post("/events", content=b"{nope", headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_non_object_body_returns_422(client):
    assert client.post("/events", json="hello").status_code == 422
    assert client.post("/events", json=[]).status_code == 422


def test_id_conflict_returns_409(client):
    client.post("/events", json=EXAMPLES[3])
    clash = dict(EXAMPLES[3], source={"id": "other", "ref": "x"})
    assert client.post("/events", json=clash).status_code == 409
