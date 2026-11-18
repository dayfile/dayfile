"""Named feeds: a saved filter plus a private or public setting, stored beside the log."""

import json
import re
import sqlite3
from typing import Any

from dayfile.render.query import QueryError, parse_query

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
TAG = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,63}$")
DEFAULT = "default"
FILTER_KEYS = ("tag", "tag_any", "status", "within")
PAGING = frozenset({"limit", "since_seq", "released_after"})
VISIBILITIES = ("private", "public")
MAX_FEEDS = 50
MAX_TITLE = 80


class FeedError(ValueError):
    """A feed definition or request was refused."""


def _pairs(spec: dict[str, Any]) -> list[list[str]]:
    """Turn {"tag": ["a", "b"], "within": "7d"} into validated query pairs."""
    pairs: list[list[str]] = []
    for key, value in spec.items():
        if key not in FILTER_KEYS:
            raise FeedError(f"unknown filter {key!r}; use {', '.join(FILTER_KEYS)}")
        values = value if isinstance(value, list) else [value]
        if not values or not all(isinstance(v, str) and v for v in values):
            raise FeedError(f"filter {key!r} needs text values")
        for v in values:
            if key in ("tag", "tag_any") and not TAG.match(v):
                raise FeedError(f"{v!r} is not a valid tag")
        pairs.extend([key, v] for v in values)
    try:
        parse_query([(k, v) for k, v in pairs])
    except QueryError as e:
        raise FeedError(str(e)) from None
    return pairs


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "name": row["name"],
        "title": row["title"],
        "filter": json.loads(row["filter"]),
        "visibility": row["visibility"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_feed(conn: sqlite3.Connection, name: str) -> dict[str, Any] | None:
    """Return the named feed, or None; the default feed always exists (private, unfiltered)."""
    row = conn.execute("SELECT * FROM feeds WHERE name = ?", (name,)).fetchone()
    if row:
        return _row(row)
    if name == DEFAULT:
        return {
            "name": DEFAULT,
            "title": None,
            "filter": [],
            "visibility": "private",
            "created_at": None,
            "updated_at": None,
        }
    return None


def list_feeds(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every saved feed in name order."""
    return [_row(r) for r in conn.execute("SELECT * FROM feeds ORDER BY name")]


def save_feed(
    conn: sqlite3.Connection,
    name: str,
    now: str,
    title: str | None = None,
    filter: dict[str, Any] | None = None,
    visibility: str | None = None,
) -> dict[str, Any]:
    """Create or update a feed; only the arguments given change, the rest keep their values."""
    if not NAME.match(name):
        raise FeedError("feed names use a-z, 0-9 and hyphens, 1 to 31 characters")
    if title is not None and len(title) > MAX_TITLE:
        raise FeedError(f"title is at most {MAX_TITLE} characters")
    if visibility is not None and visibility not in VISIBILITIES:
        raise FeedError("visibility must be private or public")
    pairs = _pairs(filter) if filter is not None else None
    if name == DEFAULT and pairs:
        raise FeedError("the default feed has no filter")
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = get_feed(conn, name)
        exists = conn.execute("SELECT 1 FROM feeds WHERE name = ?", (name,)).fetchone()
        if not exists and conn.execute("SELECT count(*) FROM feeds").fetchone()[0] >= MAX_FEEDS:
            raise FeedError(f"at most {MAX_FEEDS} feeds")
        merged = {
            "title": title if title is not None else (current or {}).get("title"),
            "filter": pairs if pairs is not None else (current or {}).get("filter", []),
            "visibility": visibility or (current or {}).get("visibility", "private"),
        }
        conn.execute(
            "INSERT INTO feeds (name, title, filter, visibility, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO UPDATE SET"
            " title = excluded.title, filter = excluded.filter,"
            " visibility = excluded.visibility, updated_at = excluded.updated_at",
            (name, merged["title"], json.dumps(merged["filter"]), merged["visibility"], now, now),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get_feed(conn, name) or {}


def delete_feed(conn: sqlite3.Connection, name: str) -> bool:
    """Remove a feed (its URL then answers 404); return False if it did not exist."""
    if name == DEFAULT:
        raise FeedError("the default feed cannot be deleted; make it private instead")
    return conn.execute("DELETE FROM feeds WHERE name = ?", (name,)).rowcount == 1
