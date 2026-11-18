import sqlite3

import pytest

from dayfile.db import connect

TABLES = ["events", "event_tags", "event_audience", "event_refs"]


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "dayfile.db")
    yield c
    c.close()


def insert_event(conn, **over):
    row = {
        "id": "01JC8Z4Q7M2X9K3T5W6N8R0VBA",
        "source_id": "test",
        "occurred_at": "2026-11-06T13:00:00Z",
        "visible_at": "2026-11-06T13:00:00Z",
        "title": "hello",
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(f"INSERT INTO events ({cols}) VALUES ({marks})", list(row.values()))
    return cur.lastrowid


def seed_children(conn):
    seq = insert_event(conn)
    conn.execute("INSERT INTO event_tags (seq, tag) VALUES (?, 'a')", (seq,))
    conn.execute("INSERT INTO event_audience (seq, label) VALUES (?, 'self')", (seq,))
    conn.execute(
        "INSERT INTO event_refs (seq, rel, target_id) VALUES (?, 'replies-to', 'X')", (seq,)
    )


def test_pragmas(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_schema_loaded_once(tmp_path):
    path = tmp_path / "d.db"
    c = connect(path)
    seed_children(c)
    c.commit()
    c.close()
    c = connect(path)  # must not reload schema or fail on existing tables
    assert c.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    c.close()


@pytest.mark.parametrize("table", TABLES)
def test_update_rejected(conn, table):
    seed_children(conn)
    sets = {
        "events": "title = 'x'",
        "event_tags": "tag = 'x'",
        "event_audience": "label = 'x'",
        "event_refs": "target_id = 'x'",
    }[table]
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute(f"UPDATE {table} SET {sets}")


@pytest.mark.parametrize("table", TABLES)
def test_delete_rejected(conn, table):
    seed_children(conn)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute(f"DELETE FROM {table}")


@pytest.mark.parametrize("col", ["occurred_at", "visible_at", "recorded_at", "expires_at"])
@pytest.mark.parametrize(
    "bad",
    [
        "2026-11-06T13:00:00+02:00",
        "2026-11-06T13:00:00",
        "2026-11-06T13:00:00.000Z",
        "2026-11-06 13:00:00Z",
    ],
)
def test_check_rejects_non_canonical_timestamp(conn, col, bad):
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(conn, **{col: bad})


def test_duplicate_source_ref_rejected(conn):
    insert_event(conn, source_ref="r1")
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(conn, id="01JC8Z5A1B2C3D4E5F6G7H8J9K", source_ref="r1")
