"""Access tokens: issue, verify, revoke. Only a hash of the secret is stored."""

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from dayfile.timefmt import minus
from dayfile.timefmt import now as clock_now

log = logging.getLogger("dayfile.auth")
LABEL = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
USE_INTERVAL = 3600  # seconds between last-use updates for one token
_DUMMY_HASH = hashlib.sha256(b"dummy").hexdigest()


@dataclass(frozen=True)
class IssuedToken:
    """A newly minted token; `value` is shown to the user once and never stored."""

    id: str
    value: str


@dataclass(frozen=True)
class Principal:
    """The identity and audience labels behind a verified credential."""

    token_id: str
    labels: frozenset[str]
    writer_sources: frozenset[str] = frozenset()  # write tokens: the source.ids they may use


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def create_token(
    conn: sqlite3.Connection,
    labels: list[str],
    *,
    expires_at: str | None = None,
    note: str | None = None,
    now: str | None = None,
    write_sources: list[str] | None = None,
) -> IssuedToken:
    """Mint a token scoped to `labels`; returns its value (`<id>.<secret>`) exactly once.

    With `write_sources` the token may POST events as those sources (and cannot read feeds).
    """
    if not labels or any(not LABEL.match(label) for label in labels):
        raise ValueError("labels must be non-empty and match [a-z][a-z0-9_-]{0,31}")
    if write_sources is not None and (
        not write_sources or any(not x.strip() for x in write_sources)
    ):
        raise ValueError("write sources must be non-empty")
    token_id, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO tokens (id, secret_hash, note, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (token_id, _hash(secret), note, now or clock_now(), expires_at),
        )
        conn.executemany(
            "INSERT INTO token_audience (token_id, label) VALUES (?, ?)",
            [(token_id, label) for label in sorted(set(labels))],
        )
        if write_sources is not None:
            conn.executemany(
                "INSERT INTO token_write (token_id, source_id) VALUES (?, ?)",
                [(token_id, x) for x in sorted(set(write_sources))],
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return IssuedToken(token_id, f"{token_id}.{secret}")


def verify_token(conn: sqlite3.Connection, value: str, now: str) -> Principal | None:
    """Return the Principal for a live token, or None. Logs ids, never values."""
    token_id, sep, secret = value.partition(".")
    if not sep or not token_id or not secret or "." in secret:
        return None
    row = conn.execute("SELECT * FROM tokens WHERE id = ?", (token_id,)).fetchone()
    expected = row["secret_hash"] if row else _DUMMY_HASH
    if not hmac.compare_digest(_hash(secret), expected) or row is None:
        if row is not None:
            log.info("token rejected: id=%s reason=bad-secret", token_id)
        return None
    if row["revoked_at"] is not None:
        log.info("token rejected: id=%s reason=revoked", token_id)
        return None
    if row["expires_at"] is not None and row["expires_at"] <= now:
        log.info("token rejected: id=%s reason=expired", token_id)
        return None
    labels = frozenset(
        r["label"]
        for r in conn.execute("SELECT label FROM token_audience WHERE token_id = ?", (token_id,))
    )
    _touch(conn, token_id, now)
    return Principal(token_id, labels, _sources(conn, token_id))


def _touch(conn: sqlite3.Connection, token_id: str, now: str) -> None:
    """Record the token's use, at most once an hour; a busy or read-only database is ignored."""
    try:
        conn.execute(
            "INSERT INTO token_use (token_id, last_used) VALUES (?, ?)"
            " ON CONFLICT (token_id) DO UPDATE SET last_used = excluded.last_used"
            " WHERE token_use.last_used <= ?",
            (token_id, now, minus(now, USE_INTERVAL)),
        )
    except sqlite3.Error:
        log.debug("last-use update skipped: id=%s", token_id)


def _sources(conn: sqlite3.Connection, token_id: str) -> frozenset[str]:
    rows = conn.execute("SELECT source_id FROM token_write WHERE token_id = ?", (token_id,))
    return frozenset(r["source_id"] for r in rows)


def revoke_token(conn: sqlite3.Connection, token_id: str, now: str) -> bool:
    """Mark a token revoked; return False if no such live token exists."""
    cur = conn.execute(
        "UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now, token_id)
    )
    return cur.rowcount == 1


def list_tokens(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Describe every token (id, labels, dates, note); secrets are not retrievable."""
    out = []
    query = (
        "SELECT t.*, u.last_used FROM tokens t LEFT JOIN token_use u ON u.token_id = t.id"
        " ORDER BY t.created_at, t.id"
    )
    for row in conn.execute(query):
        labels = conn.execute(
            "SELECT label FROM token_audience WHERE token_id = ? ORDER BY label", (row["id"],)
        )
        out.append(
            {
                "id": row["id"],
                "write_sources": sorted(_sources(conn, row["id"])),
                "labels": [r["label"] for r in labels],
                "note": row["note"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "revoked_at": row["revoked_at"],
                "last_used": row["last_used"],
            }
        )
    return out
