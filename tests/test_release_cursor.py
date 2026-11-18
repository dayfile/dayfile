"""Spec open question 6: a seq-cursor follower must not miss events released later."""

import pytest

from helpers import authed_client


class Clock:
    def __init__(self, now="2026-11-06T12:00:00Z"):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(tmp_path, clock):
    return authed_client(tmp_path / "d.db", clock=clock)


def ev(n, visible_at=None, **over):
    base = {
        "specversion": "dayfile/1",
        "id": f"01JC8Z4Q7M2X9K3T5W6N8R{n:04d}"[:26],
        "source": {"id": "t", "ref": f"r{n}"},
        "occurred_at": "2026-11-06T10:00:00Z",
        "title": f"e{n}",
    }
    if visible_at:
        base["visible_at"] = visible_at
    return {**base, **over}


def titles(doc):
    return sorted(i["title"] for i in doc["items"])


class Follower:
    """A naive cursor follower that uses only what the feed advertises."""

    def __init__(self, client, query=""):
        self.client, self.query = client, query
        self.seq, self.released = 0, None
        self.seen: list[str] = []

    def poll(self):
        while True:
            url = f"/feed.json?since_seq={self.seq}"
            if self.released:
                url += f"&released_after={self.released}"
            doc = self.client.get(url + self.query).json()
            self.seen += [i["title"] for i in doc["items"]]
            self.seq = doc["_dayfile"]["cursor"]
            self.released = doc["_dayfile"]["released_cursor"]
            if not doc.get("next_url"):
                return


def test_baseline_the_gap_exists_without_the_new_cursor(client, clock):
    client.post("/events", json=[ev(1, visible_at="2026-11-06T13:00:00Z"), ev(2)])
    first = client.get("/feed.json?since_seq=0").json()
    assert titles(first) == ["e2"]
    clock.now = "2026-11-06T13:30:00Z"
    cursor = first["_dayfile"]["cursor"]
    assert titles(client.get(f"/feed.json?since_seq={cursor}").json()) == []  # e1 is missed


def test_released_cursor_catches_the_late_release(client, clock):
    client.post("/events", json=[ev(1, visible_at="2026-11-06T13:00:00Z"), ev(2)])
    f = Follower(client)
    f.poll()
    assert f.seen == ["e2"]
    clock.now = "2026-11-06T13:30:00Z"
    f.poll()
    assert f.seen == ["e2", "e1"]
    f.poll()
    assert f.seen == ["e2", "e1"]  # and only once


def test_every_event_is_seen_exactly_once_across_pages_and_polls(client, clock):
    posts = [
        ev(1, visible_at="2026-11-06T12:30:00Z"),
        ev(2),
        ev(3, visible_at="2026-11-06T13:00:00Z"),
        ev(4),
        ev(5, visible_at="2026-11-06T12:30:00Z"),
        ev(6),
    ]
    client.post("/events", json=posts)
    f = Follower(client, "&limit=2")
    f.poll()
    for t in ("2026-11-06T12:45:00Z", "2026-11-06T13:00:00Z", "2026-11-06T14:00:00Z"):
        clock.now = t
        client.post("/events", json=ev(int(t[11:13]) * 10, visible_at=t))  # fresh arrivals too
        f.poll()
    assert sorted(f.seen) == sorted(set(f.seen))  # no duplicates
    assert {f"e{n}" for n in range(1, 7)} <= set(f.seen)  # nothing missed
    assert len(f.seen) == 6 + 3


def test_a_release_between_pages_is_not_lost_or_repeated(client, clock):
    client.post("/events", json=[ev(1, visible_at="2026-11-06T12:00:30Z"), ev(2), ev(3), ev(4)])
    first = client.get("/feed.json?since_seq=0&limit=2").json()
    assert titles(first) == ["e2", "e3"]
    clock.now = "2026-11-06T12:01:00Z"  # e1 is released while the follower is mid-traversal
    nxt = client.get(first["next_url"]).json()
    assert titles(nxt) == ["e1", "e4"]
    assert "next_url" not in nxt


def test_released_after_requires_since_seq(client):
    r = client.get("/feed.json?released_after=2026-11-06T12:00:00Z")
    assert r.status_code == 400


@pytest.mark.parametrize("bad", ["yesterday", "", "2026-13-40T00:00:00Z"])
def test_released_after_must_be_a_timestamp(client, bad):
    assert client.get(f"/feed.json?since_seq=0&released_after={bad}").status_code == 400


def test_released_after_accepts_offsets(client, clock):
    client.post("/events", json=ev(1, visible_at="2026-11-06T12:30:00Z"))
    clock.now = "2026-11-06T13:00:00Z"
    ok = client.get("/feed.json?since_seq=1&released_after=2026-11-06T14:00:00%2B02:00")
    assert titles(ok.json()) == ["e1"]  # 14:00+02:00 is 12:00Z


def test_audience_still_filters_late_releases(tmp_path):
    clock = Clock()
    owner = authed_client(tmp_path / "d.db", clock=clock, labels=("self",))
    from dayfile.auth.tokens import create_token
    from dayfile.db import connect

    c = connect(tmp_path / "d.db")
    fam = create_token(c, ["family"])
    c.close()
    owner.post("/events", json=ev(1, visible_at="2026-11-06T13:00:00Z", audience=["self"]))
    clock.now = "2026-11-06T13:30:00Z"
    r = owner.get(
        "/feed.json?since_seq=1&released_after=2026-11-06T12:00:00Z",
        headers={"Authorization": f"Bearer {fam.value}"},
    )
    assert r.json()["items"] == []


def test_next_url_carries_released_after(client, clock):
    client.post("/events", json=[ev(1), ev(2), ev(3)])
    doc = client.get("/feed.json?since_seq=0&limit=1").json()
    assert "released_after=2026-11-06T12%3A00%3A00Z" in doc["next_url"]
    assert doc["_dayfile"]["released_cursor"] == "2026-11-06T12:00:00Z"


def test_tail_mode_is_unchanged(client):
    client.post("/events", json=[ev(1), ev(2)])
    doc = client.get("/feed.json").json()
    assert "next_url" not in doc and titles(doc) == ["e1", "e2"]


def test_atom_next_link_carries_released_after(client):
    import xml.etree.ElementTree as ET

    client.post("/events", json=[ev(1), ev(2)])
    root = ET.fromstring(client.get("/feed.atom?since_seq=0&limit=1").content)
    nxt = [
        link.get("href")
        for link in root.findall("{http://www.w3.org/2005/Atom}link")
        if link.get("rel") == "next"
    ]
    assert nxt and "released_after=" in nxt[0]


def test_atom_advertises_both_cursors(client):
    import xml.etree.ElementTree as ET

    client.post("/events", json=[ev(1), ev(2)])
    root = ET.fromstring(client.get("/feed.atom?since_seq=0").content)
    assert root.find("{urn:dayfile:1}cursor").text == "2"
    assert root.find("{urn:dayfile:1}released_cursor").text == "2026-11-06T12:00:00Z"


@pytest.mark.parametrize("path", ["/feed.json", "/feed.atom", "/feed.cloudevents"])
def test_conditional_get_survives_the_clock_moving(client, clock, path):
    """released_cursor is the server's now; it must not make every response look different."""
    client.post("/events", json=[ev(1), ev(2)])
    first = client.get(path)
    clock.now = "2026-11-06T12:05:00Z"
    again = client.get(path, headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


def test_etag_changes_when_a_held_event_is_released(client, clock):
    client.post("/events", json=[ev(1, visible_at="2026-11-06T13:00:00Z"), ev(2)])
    first = client.get("/feed.json")
    clock.now = "2026-11-06T13:00:00Z"
    assert (
        client.get("/feed.json", headers={"If-None-Match": first.headers["etag"]}).status_code
        == 200
    )
