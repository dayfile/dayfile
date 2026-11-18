import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from dayfile.timefmt import minus
from helpers import authed_client

ATOM = "{http://www.w3.org/2005/Atom}"
EXAMPLES = [
    json.loads(line)
    for line in (Path(__file__).parents[1] / "examples" / "events.jsonl").read_text().splitlines()
    if line.strip()
]


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


def ev(n, **over):
    base = {
        "specversion": "dayfile/1",
        "id": f"01JC8Z4Q7M2X9K3T5W6N8R{n:04d}"[:26],
        "source": {"id": "test", "ref": f"r{n}"},
        "occurred_at": "2026-11-06T10:00:00Z",
        "title": f"event {n}",
    }
    base.update(over)
    return base


def post(client, *events):
    r = client.post("/events", json=list(events))
    assert r.status_code == 201, r.text


def titles(r):
    return [i["title"] for i in r.json()["items"]]


# --- time release and expiry -------------------------------------------------


def test_future_visible_at_is_absent_until_clock_passes_it(client, clock):
    post(client, ev(1, visible_at="2026-11-06T15:00:00Z"))
    assert titles(client.get("/feed.json")) == []
    clock.now = "2026-11-06T14:59:59Z"
    assert titles(client.get("/feed.json")) == []
    clock.now = "2026-11-06T15:00:00Z"
    assert titles(client.get("/feed.json")) == ["event 1"]


def test_expired_events_drop_out(client, clock):
    post(client, ev(1, expires_at="2026-11-06T13:00:00Z"))
    assert titles(client.get("/feed.json")) == ["event 1"]
    clock.now = "2026-11-06T13:00:00Z"
    assert titles(client.get("/feed.json")) == ["event 1"]  # not yet past
    clock.now = "2026-11-06T13:00:01Z"
    assert titles(client.get("/feed.json")) == []


def test_atom_applies_the_same_time_release(client, clock):
    post(client, ev(1, visible_at="2026-11-06T15:00:00Z"))
    assert ET.fromstring(client.get("/feed.atom").content).findall(f"{ATOM}entry") == []
    clock.now = "2026-11-06T15:00:00Z"
    assert len(ET.fromstring(client.get("/feed.atom").content).findall(f"{ATOM}entry")) == 1


# --- filters -----------------------------------------------------------------


def test_filters(client, clock):
    post(
        client,
        ev(1, tags=["todo", "a", "b"]),
        ev(2, tags=["todo", "a"]),
        ev(3, tags=["b"]),
        ev(4),
    )
    assert titles(client.get("/feed.json?tag=a&tag=b")) == ["event 1"]
    assert sorted(titles(client.get("/feed.json?tag_any=a&tag_any=b"))) == [
        "event 1",
        "event 2",
        "event 3",
    ]
    assert sorted(titles(client.get("/feed.json?tag=todo"))) == ["event 1", "event 2"]
    assert titles(client.get("/feed.json?tag=todo&tag=b&tag_any=a")) == ["event 1"]


def test_within_filters_on_visible_at(client, clock):
    post(
        client,
        ev(1, visible_at="2026-10-01T00:00:00Z"),
        ev(2, visible_at=minus(clock.now, 3 * 86400)),
        ev(3, visible_at=minus(clock.now, 3600)),
    )
    assert sorted(titles(client.get("/feed.json?within=7d"))) == ["event 2", "event 3"]
    assert titles(client.get("/feed.json?within=2h")) == ["event 3"]


def test_bad_query_is_400_and_names_the_problem(client):
    for url in ["/feed.json?within=7w", "/feed.json?limit=0", "/feed.json?status=bogus"]:
        r = client.get(url)
        assert r.status_code == 400, url
        assert "error" in r.json()
    assert client.get("/feed.atom?limit=x").status_code == 400


# --- ordering and cursor pagination -----------------------------------------


def test_newest_first_by_seq(client):
    post(client, ev(1), ev(2), ev(3))
    assert titles(client.get("/feed.json")) == ["event 3", "event 2", "event 1"]


def test_tail_mode_returns_newest_page_without_next_url(client):
    post(client, *[ev(n) for n in range(1, 8)])
    body = client.get("/feed.json?limit=3").json()
    assert [i["title"] for i in body["items"]] == ["event 7", "event 6", "event 5"]
    assert "next_url" not in body
    assert body["_dayfile"]["cursor"] == 7


def follow(client, url):
    seen = []
    while url:
        body = client.get(url).json()
        seen += [i["id"] for i in body["items"]]
        url = body.get("next_url")
    return seen


def test_following_next_url_sees_every_event_exactly_once(client):
    post(client, *[ev(n) for n in range(1, 11)])
    seen = follow(client, "/feed.json?since_seq=0&limit=3")
    assert len(seen) == 10 and len(set(seen)) == 10


def test_next_url_preserves_filters_and_ends_cleanly(client):
    post(client, *[ev(n, tags=["x"] if n % 2 else []) for n in range(1, 11)])
    first = client.get("/feed.json?since_seq=0&limit=2&tag=x").json()
    assert "tag=x" in first["next_url"] and "limit=2" in first["next_url"]
    assert len(follow(client, "/feed.json?since_seq=0&limit=2&tag=x")) == 5
    exact = client.get("/feed.json?since_seq=0&limit=5&tag=x").json()
    assert "next_url" not in exact  # exactly a full page, nothing more


def test_late_arriving_old_occurred_at_is_not_skipped(client):
    post(client, *[ev(n, occurred_at=f"2026-11-06T10:0{n}:00Z") for n in range(1, 6)])
    page1 = client.get("/feed.json?since_seq=0&limit=3").json()
    # An old-dated event arrives after the reader already fetched page 1.
    post(client, ev(9, occurred_at="2020-01-01T00:00:00Z"))
    seen = [i["id"] for i in page1["items"]] + follow(client, page1["next_url"])
    assert len(seen) == 6 and len(set(seen)) == 6
    assert any(i.endswith("0009"[-4:]) for i in seen)


def test_atom_pagination_uses_next_link(client):
    post(client, *[ev(n) for n in range(1, 6)])
    root = ET.fromstring(client.get("/feed.atom?since_seq=0&limit=2").content)
    nxt = [lk.get("href") for lk in root.findall(f"{ATOM}link") if lk.get("rel") == "next"]
    assert len(nxt) == 1 and "since_seq=" in nxt[0] and "feed.atom" in nxt[0]
    assert len(root.findall(f"{ATOM}entry")) == 2


# --- ETag ---------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/feed.json", "/feed.atom"])
def test_etag_and_conditional_get(client, clock, path):
    post(client, ev(1))
    first = client.get(path)
    etag = first.headers["etag"]
    assert etag and first.status_code == 200

    again = client.get(path, headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag

    post(client, ev(2))
    changed = client.get(path, headers={"If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag


def test_etag_changes_when_time_release_changes_content(client, clock):
    post(client, ev(1, visible_at="2026-11-06T15:00:00Z"))
    before = client.get("/feed.json").headers["etag"]
    clock.now = "2026-11-06T15:00:00Z"
    after = client.get("/feed.json")
    assert after.headers["etag"] != before and len(after.json()["items"]) == 1


def test_etag_differs_between_formats_and_queries(client):
    post(client, ev(1, tags=["a"]))
    tags = {client.get(u).headers["etag"] for u in ("/feed.json", "/feed.atom", "/feed.json?tag=a")}
    assert len(tags) == 3


def test_content_types_and_cache_header(client):
    assert client.get("/feed.json").headers["content-type"].startswith("application/feed+json")
    assert client.get("/feed.atom").headers["content-type"].startswith("application/atom+xml")
    cc = client.get("/feed.json").headers["cache-control"]
    assert "private" in cc and "no-cache" in cc


def test_examples_render_in_both_formats(client, clock):
    # By 11-09 every example is released. The weather item expired on 11-07, and the
    # retracted note and superseded todo are hidden by default (see test_lifecycle).
    clock.now = "2026-11-09T17:00:00Z"
    post(client, *EXAMPLES)
    items = client.get("/feed.json").json()["items"]
    assert len(items) == 5
    assert not any("weather" in i.get("tags", []) for i in items)
    assert len(ET.fromstring(client.get("/feed.atom").content).findall(f"{ATOM}entry")) == 5
