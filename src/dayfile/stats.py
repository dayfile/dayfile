"""Aggregate counts across tenants for the operator, computed without reading any log's content."""

import sqlite3
from pathlib import Path
from typing import Any

from dayfile.timefmt import minus

DAY = 86400
SYSTEM_SOURCES = "dayfile:%"  # events the server writes itself (heartbeats), not the user
# The only columns a stats query may read; an authorizer refuses titles, bodies, payloads, tags.
READABLE = {("events", "seq"), ("events", "source_id"), ("events", "recorded_at")}


def _authorize(action: int, arg1: str | None, arg2: str | None, *_: Any) -> int:
    if action == sqlite3.SQLITE_READ and (arg1, arg2) not in READABLE:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def open_counts_only(path: str | Path) -> sqlite3.Connection:
    """Open a tenant database read-only, able to read only the columns stats need."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.set_authorizer(_authorize)
    return conn


def log_summary(conn: sqlite3.Connection) -> tuple[int, str | None, str | None]:
    """Return (distinct user sources, first and last recorded_at) for one log."""
    sources, first, last = conn.execute(
        "SELECT COUNT(DISTINCT source_id), MIN(recorded_at), MAX(recorded_at) FROM events"
        " WHERE source_id NOT LIKE ?",
        (SYSTEM_SOURCES,),
    ).fetchone()
    return sources, first, last


def summarize(paths: dict[str, Path], now: str) -> dict[str, Any]:
    """Count logs, activated logs, multi-source logs and week-4 retention; numbers only."""
    activated = multi = cohort = retained = 0
    unreadable = 0
    for path in paths.values():
        try:
            conn = open_counts_only(path)
            try:
                sources, first, last = log_summary(conn)
            finally:
                conn.close()
        except sqlite3.Error:
            unreadable += 1
            continue
        activated += sources >= 1
        multi += sources >= 2
        if first and first <= minus(now, 28 * DAY):
            cohort += 1
            retained += bool(last and last > minus(now, 7 * DAY))
    return {
        "at": now,
        "logs": len(paths),
        "unreadable": unreadable,
        "activated": activated,
        "multi_source": multi,
        "week4": {
            "cohort": cohort,
            "retained": retained,
            "share": round(retained / cohort, 3) if cohort else None,
        },
    }
