import pytest

from dayfile import db, heartbeats
from dayfile.heartbeats import HeartbeatError
from dayfile.ingest.core import ingest_events

T0 = "2026-11-10T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "d.db")
    yield c
    c.close()


def post(conn, n, source="backup", tags=(), at=T0):
    ingest_events(
        conn,
        [
            {
                "specversion": "dayfile/1",
                "id": f"01JC8Z4Q7M2X9K3T5W6N8R{n:04d}"[:26],
                "source": {"id": source, "ref": f"r{n}"},
                "occurred_at": at,
                "title": f"event {n}",
                "tags": list(tags),
            }
        ],
    )


def at(hours):
    from dayfile.timefmt import plus

    return plus(T0, int(hours * 3600))


def events(conn):
    return [
        dict(r)
        for r in conn.execute("SELECT id, title, source_id, urgency FROM events ORDER BY seq")
    ]


def test_every_is_parsed_and_bounded():
    assert heartbeats.parse_every("24h") == 86400 and heartbeats.parse_every("7d") == 604800
    for bad in ("", "5s", "4m", "91d", "abc", 24, None):
        with pytest.raises(HeartbeatError):
            heartbeats.parse_every(bad)


def test_a_heartbeat_watches_exactly_one_of_a_source_or_a_tag(conn):
    heartbeats.save_heartbeat(conn, "backup", T0, "24h", source="backup")
    heartbeats.save_heartbeat(conn, "ci", T0, "1h", tag="ci")
    for kwargs in ({}, {"source": "a", "tag": "b"}, {"tag": "Not Valid!"}):
        with pytest.raises(HeartbeatError):
            heartbeats.save_heartbeat(conn, "x", T0, "1h", **kwargs)
    with pytest.raises(HeartbeatError):
        heartbeats.save_heartbeat(conn, "Bad Name", T0, "1h", tag="ci")
    assert [h["name"] for h in heartbeats.list_heartbeats(conn)] == ["backup", "ci"]


def test_no_event_while_the_source_is_on_time(conn):
    heartbeats.save_heartbeat(conn, "backup", T0, "24h", source="backup")
    post(conn, 1, at=T0)
    assert heartbeats.check(conn, at(25)) == []  # inside the interval plus slack
    assert [e["source_id"] for e in events(conn)] == ["backup"]


def test_a_lapse_writes_one_open_urgent_todo_and_not_another_on_the_next_check(conn):
    heartbeats.save_heartbeat(conn, "backup", T0, "24h", source="backup")
    conn.execute("UPDATE heartbeats SET updated_at = ?", (T0,))
    post(conn, 1, at=T0)
    seen = conn.execute("SELECT recorded_at FROM events").fetchone()[0]
    late = heartbeats.plus(seen, 27 * 3600)
    (title,) = heartbeats.check(conn, late)
    assert "backup" in title and "1 day" in title
    assert heartbeats.check(conn, heartbeats.plus(late, 600)) == []  # same lapse, no duplicate
    missed = [e for e in events(conn) if e["source_id"] == heartbeats.SOURCE_ID]
    assert len(missed) == 1 and missed[0]["urgency"] == 2
    tags = [r[0] for r in conn.execute("SELECT tag FROM event_tags WHERE seq = 2")]
    assert sorted(tags) == ["heartbeat", "todo"]


def test_hearing_from_the_source_again_closes_the_todo(conn):
    heartbeats.save_heartbeat(conn, "backup", T0, "24h", source="backup")
    post(conn, 1)
    seen = conn.execute("SELECT recorded_at FROM events").fetchone()[0]
    late = heartbeats.plus(seen, 30 * 3600)
    heartbeats.check(conn, late)
    # a new event arrives from the source
    post(conn, 2)
    newest = conn.execute("SELECT MAX(recorded_at) FROM events").fetchone()[0]
    assert heartbeats.check(conn, newest) == ["backup: heard from again"]
    completes = conn.execute("SELECT count(*) FROM event_refs WHERE rel = 'completes'").fetchone()
    assert completes[0] == 1
    assert heartbeats.check(conn, newest) == []  # nothing left to close


def test_a_never_heard_from_source_lapses_from_when_the_heartbeat_was_set(conn):
    heartbeats.save_heartbeat(conn, "ci", T0, "1h", tag="ci")
    assert heartbeats.check(conn, at(1.05)) == []
    (title,) = heartbeats.check(conn, at(3))
    assert title.startswith("ci: nothing from tag ci in")


def test_a_tag_heartbeat_ignores_its_own_events(conn):
    heartbeats.save_heartbeat(conn, "alerts", T0, "1h", tag="heartbeat")
    assert len(heartbeats.check(conn, at(3))) == 1
    assert heartbeats.check(conn, at(6)) == []  # its own missed event did not count as "seen"


def test_deleting_a_heartbeat_closes_what_it_left_open(conn):
    heartbeats.save_heartbeat(conn, "ci", T0, "1h", tag="ci")
    heartbeats.check(conn, at(3))
    assert heartbeats.delete_heartbeat(conn, "ci", at(4)) is True
    assert heartbeats.list_heartbeats(conn) == []
    assert (
        conn.execute("SELECT count(*) FROM event_refs WHERE rel = 'completes'").fetchone()[0] == 1
    )
    assert heartbeats.delete_heartbeat(conn, "ci", at(4)) is False


def test_an_older_database_gains_the_table_on_connect(tmp_path):
    c = db.connect(tmp_path / "old.db")
    c.executescript("DROP TABLE heartbeats; PRAGMA user_version = 10;")
    c.close()
    c = db.connect(tmp_path / "old.db")
    assert c.execute("SELECT count(*) FROM heartbeats").fetchone()[0] == 0
    c.close()
