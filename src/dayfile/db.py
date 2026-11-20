"""Connection setup, schema load, and event queries."""

import json
import os
import sqlite3
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dayfile.timefmt import minus

if TYPE_CHECKING:
    from dayfile.render.query import FeedQuery

_HERE = Path(__file__).resolve().parent
# A built wheel carries schema.sql inside the package; a source checkout keeps it at the root.
SCHEMA_PATH = (
    (_HERE / "schema.sql") if (_HERE / "schema.sql").exists() else _HERE.parents[1] / "schema.sql"
)
SCHEMA_VERSION = 11
VIEWS = ("event_state", "lifecycle_edges")  # drop order


def _create_private(path: str | Path) -> None:
    """Create a missing database file readable by its owner only (the log is private data)."""
    p = str(path)
    if p == ":memory:" or p.startswith("file:") or os.path.exists(p):
        return
    try:
        os.close(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        pass


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database with required pragmas, loading the schema if it is empty.

    Connections run in autocommit mode; writers open transactions explicitly.
    """
    _create_private(path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    if conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0:
        conn.executescript(SCHEMA_PATH.read_text())
    elif conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        _upgrade(conn)
    return conn


def _upgrade(conn: sqlite3.Connection) -> None:
    """Bring a database built by an older schema up to date; event tables are untouched."""
    text = SCHEMA_PATH.read_text()
    block = text[text.index("-- BEGIN UPGRADABLE") : text.index("-- END UPGRADABLE")]
    conn.execute("BEGIN IMMEDIATE")
    try:
        for view in VIEWS:
            conn.execute(f"DROP VIEW IF EXISTS {view}")
        for statement in _statements(block):
            conn.execute(statement)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _statements(sql: str):
    """Yield each complete SQL statement of a script, skipping comment-only chunks."""
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            if pending.strip() and not all(
                ln.strip().startswith("--") or not ln.strip() for ln in pending.splitlines()
            ):
                yield pending
            pending = ""


def find_by_source(conn: sqlite3.Connection, source_id: str, ref: str) -> int | None:
    """Return the seq of the event with this (source_id, source_ref), if any."""
    row = conn.execute(
        "SELECT seq FROM events WHERE source_id = ? AND source_ref = ?", (source_id, ref)
    ).fetchone()
    return row["seq"] if row else None


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, separators=(",", ":"))


def insert_event(conn: sqlite3.Connection, ev: dict[str, Any]) -> int:
    """Insert a validated, normalized event and its child rows; return its seq."""
    cur = conn.execute(
        "INSERT INTO events (id, source_id, source_ref, occurred_at, visible_at,"
        " expires_at, urgency, title, body, payload, links, sig)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ev["id"],
            ev["source"]["id"],
            ev["source"].get("ref"),
            ev["occurred_at"],
            ev["visible_at"],
            ev.get("expires_at"),
            ev.get("urgency", 0),
            ev["title"],
            ev.get("body"),
            _dump(ev.get("payload")),
            _dump(ev.get("links")),
            _dump(ev.get("sig")),
        ),
    )
    seq = cur.lastrowid
    conn.executemany(
        "INSERT INTO event_tags (seq, tag) VALUES (?, ?)", [(seq, t) for t in ev.get("tags", [])]
    )
    conn.executemany(
        "INSERT INTO event_audience (seq, label) VALUES (?, ?)",
        [(seq, a) for a in ev["audience"]],
    )
    conn.executemany(
        "INSERT INTO event_refs (seq, rel, target_id) VALUES (?, ?, ?)",
        [(seq, r["rel"], r["id"]) for r in ev.get("refs", [])],
    )
    return seq


def get_event(conn: sqlite3.Connection, seq: int) -> dict[str, Any]:
    """Rebuild the envelope for a stored event, plus server-assigned `seq` and `recorded_at`."""
    row = conn.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
    source = {"id": row["source_id"]}
    if row["source_ref"] is not None:
        source["ref"] = row["source_ref"]
    ev: dict[str, Any] = {
        "specversion": "dayfile/1",
        "id": row["id"],
        "source": source,
        "occurred_at": row["occurred_at"],
        "visible_at": row["visible_at"],
        "title": row["title"],
        "urgency": row["urgency"],
        "seq": row["seq"],
        "recorded_at": row["recorded_at"],
    }
    for col in ("expires_at", "body"):
        if row[col] is not None:
            ev[col] = row[col]
    for col in ("payload", "links", "sig"):
        if row[col] is not None:
            ev[col] = json.loads(row[col])
    tags = conn.execute("SELECT tag FROM event_tags WHERE seq = ? ORDER BY rowid", (seq,))
    if tag_list := [r["tag"] for r in tags]:
        ev["tags"] = tag_list
    aud = conn.execute("SELECT label FROM event_audience WHERE seq = ? ORDER BY rowid", (seq,))
    ev["audience"] = [r["label"] for r in aud]
    refs = conn.execute(
        "SELECT rel, target_id FROM event_refs WHERE seq = ? ORDER BY rowid", (seq,)
    )
    if ref_list := [{"rel": r["rel"], "id": r["target_id"]} for r in refs]:
        ev["refs"] = ref_list
    return ev


def refresh_live_edges(conn: sqlite3.Connection, now: str) -> None:
    """Fill temp.live_edges with the lifecycle edges in force at `now`.

    An edge counts when its referencing event has been released and is not itself retracted
    by a live retraction, so retracting a completion re-opens its target and retracting a
    retraction restores the original. In a retraction cycle the earliest event (lowest seq)
    wins. Call this before using STATE_CASE or STATE_CONDITIONS on the connection.
    """
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS live_edges (target_id TEXT, rel TEXT)")
    conn.execute("DELETE FROM temp.live_edges")
    edges = conn.execute(
        "SELECT target_id, rel, ref_id, ref_seq FROM lifecycle_edges WHERE ref_visible_at <= ?",
        (now,),
    ).fetchall()
    seq_of = {e["ref_id"]: e["ref_seq"] for e in edges}
    retractors: dict[str, list[str]] = {}
    for e in edges:
        if e["rel"] == "retracts":
            retractors.setdefault(e["target_id"], []).append(e["ref_id"])
    memo: dict[str, bool] = {}

    def live(node: str, stack: frozenset[str]) -> bool:
        if node not in memo:
            memo[node] = not any(
                (seq_of[z] < seq_of[node]) if z in stack else live(z, stack | {node})
                for z in retractors.get(node, ())
            )
        return memo[node]

    conn.executemany(
        "INSERT INTO temp.live_edges (target_id, rel) VALUES (?, ?)",
        [(e["target_id"], e["rel"]) for e in edges if live(e["ref_id"], frozenset())],
    )


def _tagged(marker: str) -> str:
    return f"EXISTS (SELECT 1 FROM event_tags t WHERE t.seq = e.seq AND t.tag = {marker})"


def _edge(rel: str) -> str:
    return f"EXISTS (SELECT 1 FROM temp.live_edges l WHERE l.target_id = e.id AND l.rel = '{rel}')"


# Derived state at the time of the last refresh_live_edges, precedence
# retracted > done > superseded > open. An event without the `todo` tag has no state of its own
# (NULL, left out of the output) until something closes, replaces or retracts it.
_RETRACTED, _DONE, _SUPERSEDED = _edge("retracts"), _edge("completes"), _edge("supersedes")
_TODO = "EXISTS (SELECT 1 FROM event_tags t WHERE t.seq = e.seq AND t.tag = 'todo')"
STATE_CASE = (
    f"CASE WHEN {_RETRACTED} THEN 'retracted' WHEN {_DONE} THEN 'closed'"
    f" WHEN {_SUPERSEDED} THEN 'superseded' WHEN {_TODO} THEN 'open' END"
)
STATE_CONDITIONS = {
    "retracted": _RETRACTED,
    "closed": f"(NOT {_RETRACTED} AND {_DONE})",
    "superseded": f"(NOT {_RETRACTED} AND NOT {_DONE} AND {_SUPERSEDED})",
    "open": f"(NOT {_RETRACTED} AND NOT {_DONE} AND NOT {_SUPERSEDED} AND {_TODO})",
    "none": f"(NOT {_RETRACTED} AND NOT {_DONE} AND NOT {_SUPERSEDED} AND NOT {_TODO})",
}
ADMIN_TAG = "dayfile-admin"  # token, feed and rename records the service writes to a log
DEFAULT_STATUSES = [
    "open",
    "closed",
    "none",
]  # retracted and superseded are hidden unless asked for


def query_events(
    conn: sqlite3.Connection, q: "FeedQuery", now: str, labels: Collection[str]
) -> tuple[list[dict[str, Any]], bool]:
    """Return released, unexpired events matching `q` in ascending seq, and whether more follow.

    With `since_seq` set, pages forward from that cursor. Without it, returns the newest page.
    With `released_after` as well, also returns events at or below the cursor whose release
    time falls after it, so time-held events are not missed by followers.
    Only events whose audience intersects `labels` are returned; no labels means no events.
    Each event carries its derived `status` at `now`.
    """
    refresh_live_edges(conn, now)
    params: dict[str, Any] = {"now": now}

    def bind(value: Any) -> str:
        params[f"p{len(params)}"] = value
        return f":p{len(params) - 1}"

    def bind_all(values: list[str]) -> str:
        return ", ".join(bind(v) for v in values)

    where = ["e.visible_at <= :now", "(e.expires_at IS NULL OR e.expires_at >= :now)"]
    if labels:
        where.append(
            "EXISTS (SELECT 1 FROM event_audience a WHERE a.seq = e.seq"
            f" AND a.label IN ({bind_all(sorted(labels))}))"
        )
    else:
        where.append("0")
    if q.since_seq is not None:
        after = f"e.seq > {bind(q.since_seq)}"
        if q.released_after is not None:
            # Also events at or below the cursor that were held back and released since.
            after = f"({after} OR e.visible_at > {bind(q.released_after)})"
        where.append(after)
    if q.within_seconds is not None:
        where.append(f"e.visible_at >= {bind(minus(now, q.within_seconds))}")
    if q.event_id is None and ADMIN_TAG not in q.tags and ADMIN_TAG not in q.tag_any:
        where.append(f"NOT {_tagged(bind(ADMIN_TAG))}")  # housekeeping, shown on request
    if q.tags:
        tags = sorted(set(q.tags))
        where.append(
            "(SELECT count(*) FROM event_tags t WHERE t.seq = e.seq"
            f" AND t.tag IN ({bind_all(tags)})) = {bind(len(tags))}"
        )
    if q.tag_any:
        where.append(
            "EXISTS (SELECT 1 FROM event_tags t WHERE t.seq = e.seq"
            f" AND t.tag IN ({bind_all(q.tag_any)}))"
        )
    if q.event_id is not None:
        where.append(f"e.id = {bind(q.event_id)}")
    wanted = q.statuses or (list(STATE_CONDITIONS) if q.event_id else DEFAULT_STATUSES)
    where.append("(" + " OR ".join(STATE_CONDITIONS[s] for s in dict.fromkeys(wanted)) + ")")
    forward = q.since_seq is not None
    sql = (
        f"SELECT e.seq, {STATE_CASE} AS state FROM events e WHERE {' AND '.join(where)}"
        f" ORDER BY e.seq {'ASC' if forward else 'DESC'} LIMIT {bind(q.limit + forward)}"
    )
    rows = sorted(conn.execute(sql, params).fetchall(), key=lambda r: r["seq"])
    has_more = forward and len(rows) > q.limit
    rows = rows[: q.limit] if forward else rows
    return [_with_state(get_event(conn, r["seq"]), r["state"]) for r in rows], has_more


def events_between(
    conn: sqlite3.Connection, start: str, end: str, now: str, labels: Collection[str]
) -> list[dict[str, Any]]:
    """Events released at `now` with `start <= visible_at < end`, visible to `labels`.

    Includes events that have since expired and skips `digest.daily` so digests never nest.
    Each event carries its derived `status`.
    """
    refresh_live_edges(conn, now)
    marks = ", ".join(f":l{i}" for i in range(len(labels)))
    params: dict[str, Any] = {"start": start, "end": end, "now": now}
    params.update({f"l{i}": label for i, label in enumerate(sorted(labels))})
    sql = (
        f"SELECT e.seq, {STATE_CASE} AS state FROM events e"
        " WHERE e.visible_at >= :start AND e.visible_at < :end AND e.visible_at <= :now"
        " AND e.source_id <> 'gen:digest'"
        f" AND NOT {_tagged(repr(ADMIN_TAG))}"
        " AND EXISTS (SELECT 1 FROM event_audience a WHERE a.seq = e.seq"
        f" AND a.label IN ({marks or 'NULL'})) ORDER BY e.seq"
    )
    return [_with_state(get_event(conn, r["seq"]), r["state"]) for r in conn.execute(sql, params)]


def _with_state(event: dict[str, Any], state: str | None) -> dict[str, Any]:
    """Add the derived `status`, left out for an event that has none."""
    return event if state is None else {**event, "status": state}


def _marks(values: list[str]) -> str:
    return ", ".join("?" for _ in values)
