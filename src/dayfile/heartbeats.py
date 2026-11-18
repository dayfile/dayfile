"""Heartbeats: say how often a source or tag should be heard from, and get an event when it isn't.

A heartbeat is a setting (like a feed), not an event. `check` compares it with the log and writes
ordinary events: a to-do tagged `heartbeat` when the expected cadence lapses, and a closing event
when the source is heard from again. The log stays the one place to look.
"""

import re
import sqlite3
from typing import Any

from dayfile.feeds import NAME, TAG
from dayfile.ingest.core import ingest_events
from dayfile.timefmt import plus
from dayfile.ulid import new_ulid

SOURCE_ID = "dayfile:heartbeat"
MIN_EVERY, MAX_EVERY = 300, 90 * 86400
MAX_HEARTBEATS = 20
UNITS = {"m": 60, "h": 3600, "d": 86400}
EVERY = re.compile(r"^(\d{1,4})([mhd])$")


class HeartbeatError(ValueError):
    """A heartbeat definition was refused."""


def parse_every(text: Any) -> int:
    """Turn "30m", "24h" or "7d" into seconds, within 5 minutes to 90 days."""
    found = EVERY.match(text.strip().lower()) if isinstance(text, str) else None
    seconds = int(found.group(1)) * UNITS[found.group(2)] if found else 0
    if not MIN_EVERY <= seconds <= MAX_EVERY:
        raise HeartbeatError("every must look like 30m, 24h or 7d, from 5 minutes to 90 days")
    return seconds


def show_every(seconds: int) -> str:
    """An exact spelling of an interval, in hours up to two days: "24h", "90m", "7d"."""
    for unit in ("d", "h", "m"):
        if seconds % UNITS[unit] == 0 and (unit != "d" or seconds > 2 * 86400):
            return f"{seconds // UNITS[unit]}{unit}"
    return f"{seconds}s"


def grace(seconds: int) -> int:
    """Slack allowed after the interval: a tenth of it, at least a minute."""
    return max(60, seconds // 10)


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "every": show_every(row["every_seconds"]),
        "source": row["source"],
        "tag": row["tag"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_heartbeats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every heartbeat in name order."""
    return [_row(r) for r in conn.execute("SELECT * FROM heartbeats ORDER BY name")]


def save_heartbeat(
    conn: sqlite3.Connection,
    name: str,
    now: str,
    every: Any,
    source: str | None = None,
    tag: str | None = None,
) -> dict[str, Any]:
    """Create or replace a heartbeat that watches one source id or one tag."""
    if not NAME.match(name):
        raise HeartbeatError("names use a-z, 0-9 and hyphens, 1 to 31 characters")
    seconds = parse_every(every)
    source, tag = (source or "").strip() or None, (tag or "").strip().lower() or None
    if (source is None) == (tag is None):
        raise HeartbeatError("watch either a source or a tag, not both and not neither")
    if source is not None and (len(source) > 100 or source == SOURCE_ID):
        raise HeartbeatError("that source cannot be watched")
    if tag is not None and not TAG.match(tag):
        raise HeartbeatError(f"{tag!r} is not a valid tag")
    conn.execute("BEGIN IMMEDIATE")
    try:
        known = conn.execute("SELECT 1 FROM heartbeats WHERE name = ?", (name,)).fetchone()
        if not known and conn.execute("SELECT count(*) FROM heartbeats").fetchone()[0] >= (
            MAX_HEARTBEATS
        ):
            raise HeartbeatError(f"at most {MAX_HEARTBEATS} heartbeats")
        conn.execute(
            "INSERT INTO heartbeats (name, every_seconds, source, tag, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET"
            " every_seconds = excluded.every_seconds, source = excluded.source,"
            " tag = excluded.tag, updated_at = excluded.updated_at",
            (name, seconds, source, tag, now, now),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return next(h for h in list_heartbeats(conn) if h["name"] == name)


def delete_heartbeat(conn: sqlite3.Connection, name: str, now: str) -> bool:
    """Remove a heartbeat and close any missed-heartbeat to-do it left open."""
    row = conn.execute("SELECT 1 FROM heartbeats WHERE name = ?", (name,)).fetchone()
    if row is None:
        return False
    _close_open(conn, name, now, "removed")
    conn.execute("DELETE FROM heartbeats WHERE name = ?", (name,))
    return True


def _last_seen(conn: sqlite3.Connection, hb: sqlite3.Row, now: str) -> str | None:
    """When the log last recorded a visible event from the thing the heartbeat watches."""
    if hb["source"] is not None:
        sql = "SELECT MAX(recorded_at) FROM events WHERE source_id = ? AND visible_at <= ?"
        args: tuple[Any, ...] = (hb["source"], now)
    else:
        sql = (
            "SELECT MAX(e.recorded_at) FROM events e JOIN event_tags t ON t.seq = e.seq"
            " WHERE t.tag = ? AND e.source_id != ? AND e.visible_at <= ?"
        )
        args = (hb["tag"], SOURCE_ID, now)
    return conn.execute(sql, args).fetchone()[0]


def _open_missed(conn: sqlite3.Connection, name: str) -> list[str]:
    """Ids of this heartbeat's missed-heartbeat events that nothing has completed yet."""
    return [
        r["id"]
        for r in conn.execute(
            "SELECT e.id FROM events e WHERE e.source_id = ? AND e.source_ref LIKE ?"
            " AND NOT EXISTS (SELECT 1 FROM event_refs r"
            " WHERE r.target_id = e.id AND r.rel = 'completes')",
            (SOURCE_ID, name + "@%"),  # names hold no LIKE wildcards
        )
    ]


def _close_open(conn: sqlite3.Connection, name: str, now: str, why: str) -> int:
    ids = _open_missed(conn, name)
    if ids:
        ingest_events(
            conn,
            [
                {
                    "specversion": "dayfile/1",
                    "id": new_ulid(),
                    "source": {"id": SOURCE_ID, "ref": f"{name}:{why}:{target}"},
                    "occurred_at": now,
                    "refs": [{"rel": "completes", "id": target}],
                }
                for target in ids
            ],
        )
    return len(ids)


def _plain(seconds: int) -> str:
    """A lapse in words: "26 hours", "3 days", "40 minutes"."""
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return f"{seconds} seconds"


def check(conn: sqlite3.Connection, now: str) -> list[str]:
    """Write the events the heartbeats call for at `now`; return their titles."""
    written: list[str] = []
    for hb in conn.execute("SELECT * FROM heartbeats ORDER BY name").fetchall():
        seen = _last_seen(conn, hb, now)
        baseline = seen or hb["updated_at"]
        every = hb["every_seconds"]
        if now <= plus(baseline, every + grace(every)):
            if _close_open(conn, hb["name"], now, "ok"):
                written.append(f"{hb['name']}: heard from again")
            continue
        what = hb["source"] or f"tag {hb['tag']}"
        title = f"{hb['name']}: nothing from {what} in {_plain(_age(baseline, now))}"
        result = ingest_events(
            conn,
            [
                {
                    "specversion": "dayfile/1",
                    "id": new_ulid(),
                    "source": {"id": SOURCE_ID, "ref": f"{hb['name']}@{baseline}"},
                    "occurred_at": now,
                    "title": title,
                    "body": f"Expected an event every {show_every(every)}. Last seen: "
                    + (seen or "never since the heartbeat was set"),
                    "tags": ["todo", "heartbeat"],
                    "urgency": 2,
                    "payload": {
                        "heartbeat": hb["name"],
                        "expected_every": show_every(every),
                        "last_seen": seen,
                    },
                }
            ],
        )
        if result[0].created:
            written.append(title)
    return written


def _age(then: str, now: str) -> int:
    return int(_epoch(now) - _epoch(then))


def _epoch(stamp: str) -> float:
    from datetime import UTC, datetime

    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def check_all(tenancy: Any, now: str) -> dict[str, list[str]]:
    """Run `check` on every live tenant that has heartbeats; return titles by tenant."""
    from dayfile import db

    out: dict[str, list[str]] = {}
    for slug in tenancy.list():
        tenant = tenancy.get(slug)
        if tenant is None or tenant.suspended:
            continue
        conn = db.connect(tenant.db_path)
        try:
            if conn.execute("SELECT 1 FROM heartbeats LIMIT 1").fetchone():
                if written := check(conn, now):
                    out[slug] = written
        finally:
            conn.close()
    return out
