import json
from pathlib import Path

import pytest

from dayfile.db import connect
from dayfile.ingest.core import ConflictError, IngestError, ingest_events

EXAMPLES = [
    json.loads(line)
    for line in (Path(__file__).parents[1] / "examples" / "events.jsonl").read_text().splitlines()
    if line.strip()
]


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "d.db")
    yield c
    c.close()


def minimal(**over):
    ev = {
        "specversion": "dayfile/1",
        "id": "01JC8Z4Q7M2X9K3T5W6N8R0VBA",
        "source": {"id": "test", "ref": "r1"},
        "occurred_at": "2026-11-06T13:00:00Z",
        "title": "hi",
    }
    ev.update(over)
    return ev


def count(conn, table="events"):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_defaults_are_filled(conn):
    [res] = ingest_events(conn, [minimal()])
    ev = res.event
    assert res.created
    assert ev["visible_at"] == "2026-11-06T13:00:00Z"
    assert ev["audience"] == ["self"]
    assert ev["seq"] == 1
    assert ev["recorded_at"].endswith("Z")


def test_empty_audience_means_self(conn):
    [res] = ingest_events(conn, [minimal(audience=[])])
    assert res.event["audience"] == ["self"]


def test_offsets_are_normalized(conn):
    ev = minimal(occurred_at="2026-11-06T15:00:00+02:00", visible_at="2026-11-06T18:00:00+02:00")
    [res] = ingest_events(conn, [ev])
    assert res.event["occurred_at"] == "2026-11-06T13:00:00Z"
    assert res.event["visible_at"] == "2026-11-06T16:00:00Z"


def test_naive_time_rejected_and_nothing_written(conn):
    with pytest.raises(IngestError):
        ingest_events(conn, [minimal(occurred_at="2026-11-06T13:00:00")])
    assert count(conn) == 0


def test_children_are_stored(conn):
    ev = minimal(
        tags=["a", "b"],
        audience=["self", "family"],
        refs=[{"rel": "replies-to", "id": "01JC8Z5A1B2C3D4E5F6G7H8J9K"}],
        payload={"due": "2026-11-14"},
        links=[{"rel": "related", "href": "https://example.com"}],
        urgency=2,
    )
    [res] = ingest_events(conn, [ev])
    out = res.event
    assert sorted(out["tags"]) == ["a", "b"]
    assert sorted(out["audience"]) == ["family", "self"]
    assert out["refs"] == ev["refs"]
    assert out["payload"] == ev["payload"]
    assert out["links"] == ev["links"]
    assert out["urgency"] == 2
    assert count(conn, "event_tags") == 2
    assert count(conn, "event_audience") == 2
    assert count(conn, "event_refs") == 1


def test_redelivery_is_a_noop_returning_existing(conn):
    [first] = ingest_events(conn, [minimal()])
    [second] = ingest_events(conn, [minimal(id="01JC8Z5A1B2C3D4E5F6G7H8J9K", title="changed")])
    assert not second.created
    assert second.event == first.event
    assert count(conn) == 1
    assert count(conn, "event_audience") == 1


def test_duplicate_within_one_batch(conn):
    a, b = ingest_events(conn, [minimal(), minimal(id="01JC8Z5A1B2C3D4E5F6G7H8J9K")])
    assert a.created and not b.created
    assert a.event["seq"] == b.event["seq"]
    assert count(conn) == 1


def test_no_ref_means_no_idempotency(conn):
    a = minimal(source={"id": "test"})
    b = minimal(id="01JC8Z5A1B2C3D4E5F6G7H8J9K", source={"id": "test"})
    ingest_events(conn, [a, b])
    assert count(conn) == 2


def test_examples_twice_yield_8_rows(conn):
    ingest_events(conn, EXAMPLES)
    ingest_events(conn, EXAMPLES)
    assert count(conn) == 8


def test_malformed_event_in_batch_writes_nothing(conn):
    bad = minimal(id="01JC8Z5A1B2C3D4E5F6G7H8J9K", source={"id": "test", "ref": "r2"})
    del bad["title"]
    with pytest.raises(IngestError) as exc:
        ingest_events(conn, [minimal(), bad])
    assert "title" in str(exc.value)
    assert count(conn) == 0
    assert count(conn, "event_audience") == 0


def test_id_reused_with_other_source_conflicts_and_writes_nothing(conn):
    ingest_events(conn, [minimal()])
    other = minimal(source={"id": "test", "ref": "other"})
    fresh = minimal(id="01JC8Z5A1B2C3D4E5F6G7H8J9K", source={"id": "test", "ref": "r9"})
    with pytest.raises(ConflictError):
        ingest_events(conn, [fresh, other])
    assert count(conn) == 1
