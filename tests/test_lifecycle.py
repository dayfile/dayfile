import json
from pathlib import Path

import pytest

from dayfile.db import connect
from dayfile.ingest.core import ingest_events
from helpers import authed_client

EXAMPLES = [
    json.loads(line)
    for line in (Path(__file__).parents[1] / "examples" / "events.jsonl").read_text().splitlines()
    if line.strip()
]
ALL = "status=open&status=closed&status=superseded&status=retracted&status=none"


class Clock:
    def __init__(self, now="2026-11-09T17:00:00Z"):
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
def client(db_path, clock):
    return authed_client(db_path, clock=clock, labels=("self", "family"))


def eid(n):
    return f"01JC8Z4Q7M2X9K3T5W6N8R{n:04d}"


def ev(n, source="a", **over):
    base = {
        "specversion": "dayfile/1",
        "id": eid(n),
        "tags": ["todo"],
        "source": {"id": source, "ref": f"r{n}"},
        "occurred_at": "2026-11-06T10:00:00Z",
        "title": f"event {n}",
    }
    base.update(over)
    return base


def lc(n, rel, target, **over):
    return ev(n, **{"tags": [], "refs": [{"rel": rel, "id": eid(target)}], **over})


def post(client, *events):
    r = client.post("/events", json=list(events))
    assert r.status_code == 201, r.text


def statuses(client, query=ALL):
    items = client.get(f"/feed.json?{query}").json()["items"]
    return {i["title"]: i["_dayfile"].get("status", "none") for i in items}


def titles(client, query=""):
    return {i["title"] for i in client.get(f"/feed.json?{query}").json()["items"]}


# --- the roadmap examples ----------------------------------------------------


def test_examples_change_feed_output(client):
    post(client, *EXAMPLES)
    assert statuses(client) == {
        "Renew passport": "closed",
        "Done: Renew passport": "none",
        "Idea: feed algebra": "retracted",
        "Retracted note": "none",
        "Call plumber": "superseded",
        "Call plumber (moved)": "open",
        "Edge node 07: vibration above threshold": "none",
    }  # the weather item has expired
    # By default retracted and superseded are hidden; done stays visible.
    assert titles(client) == {
        "Renew passport",
        "Done: Renew passport",
        "Retracted note",
        "Call plumber (moved)",
        "Edge node 07: vibration above threshold",
    }


def test_status_filters(client):
    post(client, *EXAMPLES)
    assert titles(client, "status=retracted") == {"Idea: feed algebra"}
    assert titles(client, "status=superseded") == {"Call plumber"}
    assert titles(client, "status=closed") == {"Renew passport"}
    assert "Renew passport" not in titles(client, "status=open")
    assert titles(client, "status=closed&status=retracted") == {
        "Renew passport",
        "Idea: feed algebra",
    }


def test_lifecycle_events_take_effect_only_once_released(client, clock):
    post(client, *EXAMPLES)
    clock.now = "2026-11-08T12:00:00Z"  # the todo.done is dated 11-09
    assert statuses(client)["Renew passport"] == "open"
    clock.now = "2026-11-09T16:30:00Z"
    assert statuses(client)["Renew passport"] == "closed"


def test_rows_never_change(client, db_path):
    post(client, ev(1))
    conn = connect(db_path)
    before = [tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY seq")]
    post(client, lc(2, "completes", 1), lc(3, "retracts", 1))
    after = [tuple(r) for r in conn.execute("SELECT * FROM events ORDER BY seq")]
    assert after[:1] == before
    conn.close()


# --- state rules -------------------------------------------------------------


def test_precedence_retracted_over_done_over_superseded(client):
    post(client, ev(1), ev(2), ev(3))
    post(client, lc(11, "completes", 1), lc(12, "supersedes", 1))
    post(client, lc(13, "retracts", 2), lc(14, "completes", 2))
    post(client, lc(15, "supersedes", 3))
    s = statuses(client)
    assert (s["event 1"], s["event 2"], s["event 3"]) == ("closed", "retracted", "superseded")


def test_replies_to_changes_nothing(client):
    post(client, ev(1), ev(2, refs=[{"rel": "replies-to", "id": eid(1)}]))
    assert statuses(client)["event 1"] == "open"


def test_ref_to_a_target_that_arrives_later_takes_effect(client):
    post(client, lc(2, "completes", 1))
    assert "event 1" not in statuses(client)
    post(client, ev(1))
    assert statuses(client)["event 1"] == "closed"


def test_a_supersession_chain_marks_every_old_version(client):
    post(client, ev(1), ev(2), ev(3))
    post(client, lc(12, "supersedes", 1), lc(13, "supersedes", 2))
    s = statuses(client)
    assert (s["event 1"], s["event 2"]) == ("superseded", "superseded")


def test_self_reference_is_ignored(client):
    post(client, ev(1, refs=[{"rel": "retracts", "id": eid(1)}]))
    assert statuses(client)["event 1"] == "open"


# --- authorization -----------------------------------------------------------


def test_other_source_with_disjoint_audience_has_no_effect(client):
    post(client, ev(1, source="a", audience=["family"]))
    post(client, lc(2, "retracts", 1, source="b"))  # audience defaults to self
    assert statuses(client)["event 1"] == "open"


def test_same_source_is_authorized_even_with_a_different_audience(client):
    post(client, ev(1, source="a", audience=["family"]))
    post(client, lc(2, "completes", 1, source="a"))
    assert statuses(client)["event 1"] == "closed"


def test_shared_audience_label_authorizes_another_source(client):
    post(client, ev(1, source="a", audience=["self", "family"]))
    post(client, lc(2, "completes", 1, source="b", audience=["family"]))
    assert statuses(client)["event 1"] == "closed"


# --- schema pieces -----------------------------------------------------------


def test_edge_authorization_in_effective_state(tmp_path):
    from dayfile.render.query import FeedQuery

    conn = connect(tmp_path / "d.db")
    ingest_events(
        conn,
        [
            ev(1, source="a", audience=["family"]),
            lc(2, "retracts", 1, source="b"),  # unauthorized
            ev(3, source="a"),
            lc(4, "completes", 3, source="a"),  # authorized
        ],
    )
    from dayfile.db import query_events

    q = FeedQuery(statuses=["open", "closed", "retracted"])
    events, _ = query_events(conn, q, "2026-11-09T17:00:00Z", {"self", "family"})
    state = {e["id"]: e["status"] for e in events}
    assert state[eid(1)] == "open"
    assert state[eid(3)] == "closed"
    conn.close()


def test_connect_refreshes_views_of_an_older_database(tmp_path):
    path = tmp_path / "old.db"
    conn = connect(path)
    conn.executescript(
        """
        DROP VIEW lifecycle_edges;
        CREATE VIEW lifecycle_edges AS SELECT 1 AS target_id;
        PRAGMA user_version = 0;
        """
    )
    conn.close()
    conn = connect(path)
    conn.execute("SELECT * FROM lifecycle_edges").fetchall()
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 2
    conn.close()


# --- reversing lifecycle events (spec open question 7) -------------------------


def tomb(n, target, **over):
    return ev(n, **{"tags": [], "refs": [{"rel": "retracts", "id": eid(target)}], **over})


def test_retracting_a_completion_reopens_the_todo(client):
    post(client, ev(1), lc(2, "completes", 1))
    assert statuses(client)["event 1"] == "closed"
    post(client, tomb(3, 2))
    s = statuses(client)
    assert s["event 1"] == "open"
    assert s["event 2"] == "retracted"
    assert s["event 3"] == "none"  # a tombstone is not tagged todo, so it has no state of its own


def test_retracting_a_retraction_restores_the_original_retraction(client):
    post(client, ev(1), tomb(2, 1))
    assert statuses(client)["event 1"] == "retracted"
    post(client, tomb(3, 2))  # undo the retraction
    assert statuses(client)["event 1"] == "open"
    post(client, tomb(4, 3))  # undo the undo
    assert statuses(client)["event 1"] == "retracted"


def test_retracting_a_superseding_event_unsupersedes(client):
    post(client, ev(1), lc(2, "supersedes", 1))
    assert statuses(client)["event 1"] == "superseded"
    post(client, tomb(3, 2))
    assert statuses(client)["event 1"] == "open"


def test_a_retraction_that_is_not_released_yet_changes_nothing(client, clock):
    post(client, ev(1), lc(2, "completes", 1), tomb(3, 2, visible_at="2026-11-10T00:00:00Z"))
    assert statuses(client)["event 1"] == "closed"
    clock.now = "2026-11-10T00:00:00Z"
    assert statuses(client)["event 1"] == "open"


def test_unauthorized_retraction_of_a_completion_has_no_effect(client):
    post(
        client,
        ev(1, source="a", audience=["self"]),
        lc(2, "completes", 1, source="a", audience=["self"]),
        tomb(3, 2, source="b", audience=["family"]),  # other source, no shared label
    )
    assert statuses(client)["event 1"] == "closed"


def test_mutual_retraction_cycle_resolves_in_favour_of_the_earlier_event(client):
    post(client, ev(1), ev(2))
    # event 3 retracts event 4, event 4 retracts event 3 (4 refers to 3, 3 refers ahead)
    post(client, tomb(3, 4), tomb(4, 3))
    s = statuses(client)
    assert s["event 3"] == "none" and s["event 4"] == "retracted"


def test_longer_cycle_is_deterministic(client):
    post(client, tomb(1, 3), tomb(2, 1), tomb(3, 2))  # 1 -> 3 -> 2 -> 1
    first = statuses(client)
    assert statuses(client) == first
    assert sorted(first.values()).count("none") >= 1


def test_state_matches_the_notify_view_of_open(tmp_path):
    """The notifier must treat a re-opened todo as open, same as the feed."""
    from dayfile.db import STATE_CONDITIONS, refresh_live_edges

    conn = connect(tmp_path / "d.db")
    ingest_events(conn, [ev(1), lc(2, "completes", 1), tomb(3, 2)])
    refresh_live_edges(conn, "2026-11-09T17:00:00Z")
    row = conn.execute(
        f"SELECT count(*) FROM events e WHERE e.id = :id AND {STATE_CONDITIONS['open']}",
        {"id": eid(1), "now": "2026-11-09T17:00:00Z"},
    ).fetchone()
    assert row[0] == 1
