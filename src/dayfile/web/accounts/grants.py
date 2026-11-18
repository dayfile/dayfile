"""Assistant connections (MCP over OAuth): registered clients, one-time codes and refresh grants.

Secrets (codes and refresh tokens) are stored only as hashes. A refresh token works once: using
it again, or using the one it replaced, ends the whole connection.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sqlite3
from typing import Any
from urllib.parse import urlsplit

from dayfile.timefmt import minus, plus

CODE_SECONDS = 300
REFRESH_SECONDS = 60 * 86400  # slides forward each time the refresh token is used
GRANT_MAX_SECONDS = 365 * 86400
MAX_CLIENTS = 5000
MAX_REDIRECTS = 5
VERIFIER = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")  # base64url of a SHA-256 digest
LOOPBACK = ("localhost", "127.0.0.1", "::1")
HOSTNAME = re.compile(r"[a-z0-9.-]+|[0-9a-f:]+")  # a name or an IPv6 address, nothing else


class ClientError(ValueError):
    """The registration request is not acceptable (the message is safe to show)."""


def _digest(raw: str) -> bytes:
    return hashlib.sha256(raw.encode()).digest()


def is_loopback(host: str | None) -> bool:
    if host in LOOPBACK:
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def check_redirect(uri: Any) -> str:
    """Accept an https URL, or an http one on the loopback interface; return it or raise."""
    if not isinstance(uri, str) or not 8 <= len(uri) <= 300 or not uri.isascii():
        raise ClientError("redirect_uris must be short URLs")
    if any(c.isspace() or ord(c) < 32 for c in uri):
        raise ClientError("redirect_uris must not contain spaces")
    parts = urlsplit(uri)
    try:
        host = parts.hostname
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError as e:
        raise ClientError("redirect_uris has a bad port") from e
    if not host or not HOSTNAME.fullmatch(host):
        raise ClientError("redirect_uris has a bad host")
    secure = parts.scheme == "https"
    loopback = parts.scheme == "http" and is_loopback(host)
    if not (secure or loopback) or parts.username or parts.password or parts.fragment:
        raise ClientError("redirect_uris must be https, or http on localhost, with no fragment")
    return uri


def register_client(
    conn: sqlite3.Connection, now: str, name: Any, redirect_uris: Any
) -> dict[str, Any]:
    """Create a public client (RFC 7591); returns its stored description."""
    if not isinstance(redirect_uris, list) or not 1 <= len(redirect_uris) <= MAX_REDIRECTS:
        raise ClientError(f"redirect_uris must list 1 to {MAX_REDIRECTS} URLs")
    uris = [check_redirect(u) for u in redirect_uris]
    label = re.sub(r"[^\w .,'()/+-]", "", name if isinstance(name, str) else "")[:60].strip()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM mcp_client WHERE created_at < ? AND id NOT IN"
            " (SELECT client_id FROM mcp_grant) AND id NOT IN (SELECT client_id FROM mcp_code)",
            (minus(now, 30 * 86400),),
        )
        count = conn.execute("SELECT count(*) FROM mcp_client").fetchone()[0]
        if count >= MAX_CLIENTS:
            raise ClientError("too many registered clients; try again later")
        client_id = "dfc_" + secrets.token_urlsafe(16)
        label = label or "An assistant"
        conn.execute(
            "INSERT INTO mcp_client (id, name, redirect_uris, created_at) VALUES (?, ?, ?, ?)",
            (client_id, label, json.dumps(uris), now),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"id": client_id, "name": label, "redirect_uris": uris}


def get_client(conn: sqlite3.Connection, client_id: str) -> dict[str, Any] | None:
    """A registered client, or None."""
    row = conn.execute("SELECT * FROM mcp_client WHERE id = ?", (client_id[:100],)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"], "redirect_uris": json.loads(row["redirect_uris"])}


def redirect_matches(client: dict[str, Any], uri: str) -> bool:
    """True if `uri` is one of the client's; loopback addresses may differ in port (RFC 8252)."""
    target = urlsplit(uri)
    for registered in client["redirect_uris"]:
        if registered == uri:
            return True
        known = urlsplit(registered)
        if (
            is_loopback(known.hostname)
            and known.scheme == target.scheme == "http"
            and known.hostname == target.hostname
            and known.path == target.path
            and known.query == target.query
        ):
            return True
    return False


def pkce_ok(verifier: str, challenge: str) -> bool:
    """True if `verifier` hashes (S256) to `challenge`."""
    if not VERIFIER.fullmatch(verifier):
        return False
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return hmac.compare_digest(expected, challenge)


def create_code(
    conn: sqlite3.Connection,
    now: str,
    client_id: str,
    account_id: str,
    redirect_uri: str,
    challenge: str,
    can_write: bool,
) -> str:
    """Issue a one-time authorization code for an approved request."""
    conn.execute("DELETE FROM mcp_code WHERE expires_at <= ?", (now,))
    raw = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO mcp_code VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            _digest(raw),
            client_id,
            account_id,
            redirect_uri,
            challenge,
            int(can_write),
            plus(now, CODE_SECONDS),
        ),
    )
    return raw


def redeem_code(
    conn: sqlite3.Connection, now: str, raw: str, client_id: str, redirect_uri: str, verifier: str
) -> dict[str, Any] | None:
    """Spend a code (it works once whatever the outcome) and return who approved what, or None."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM mcp_code WHERE code_hash = ?", (_digest(raw[:200]),)
        ).fetchone()
        if row is not None:
            conn.execute("DELETE FROM mcp_code WHERE code_hash = ?", (row["code_hash"],))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if (
        row is None
        or row["expires_at"] <= now
        or row["client_id"] != client_id
        or row["redirect_uri"] != redirect_uri
        or not pkce_ok(verifier, row["challenge"])
    ):
        return None
    return {"account_id": row["account_id"], "can_write": bool(row["can_write"])}


def create_grant(
    conn: sqlite3.Connection,
    now: str,
    client_id: str,
    account_id: str,
    can_write: bool,
    token_ids: list[str],
) -> tuple[str, str]:
    """Start a connection; returns its id and its first refresh token."""
    grant_id, refresh = secrets.token_hex(8), secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO mcp_grant (id, client_id, account_id, can_write, created_at, last_used,"
        " refresh_hash, refresh_expires_at, token_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            grant_id,
            client_id,
            account_id,
            int(can_write),
            now,
            now,
            _digest(refresh),
            plus(now, REFRESH_SECONDS),
            json.dumps(token_ids),
        ),
    )
    return grant_id, refresh


def find_by_refresh(
    conn: sqlite3.Connection, now: str, raw: str, client_id: str
) -> tuple[str, dict[str, Any] | None]:
    """Look a refresh token up: ("ok", grant), ("reused", grant) or ("none", None).

    A token that was already replaced is evidence of theft, so its grant is deleted at once and
    returned so the caller can revoke what it issued.
    """
    digest = _digest(raw[:200])
    row = conn.execute(
        "SELECT * FROM mcp_grant WHERE refresh_hash = ? OR prev_hash = ?", (digest, digest)
    ).fetchone()
    if row is None or row["client_id"] != client_id:
        return "none", None
    grant = dict(row, token_ids=json.loads(row["token_ids"]))
    if row["refresh_hash"] != digest:
        conn.execute("DELETE FROM mcp_grant WHERE id = ?", (row["id"],))
        return "reused", grant
    if row["refresh_expires_at"] <= now or plus(row["created_at"], GRANT_MAX_SECONDS) <= now:
        conn.execute("DELETE FROM mcp_grant WHERE id = ?", (row["id"],))
        return "none", None
    return "ok", grant


def rotate(
    conn: sqlite3.Connection, now: str, grant: dict[str, Any], old_raw: str, token_ids: list[str]
) -> str | None:
    """Swap in a new refresh token and the newly issued token ids; None if it lost a race."""
    new = secrets.token_urlsafe(32)
    cur = conn.execute(
        "UPDATE mcp_grant SET refresh_hash = ?, prev_hash = ?, refresh_expires_at = ?,"
        " last_used = ?, token_ids = ? WHERE id = ? AND refresh_hash = ?",
        (
            _digest(new),
            _digest(old_raw[:200]),
            plus(now, REFRESH_SECONDS),
            now,
            json.dumps(token_ids),
            grant["id"],
            _digest(old_raw[:200]),
        ),
    )
    return new if cur.rowcount == 1 else None


def list_grants(conn: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
    """An account's connections, newest first."""
    rows = conn.execute(
        "SELECT g.id, g.can_write, g.created_at, g.last_used, c.name FROM mcp_grant g"
        " JOIN mcp_client c ON c.id = g.client_id WHERE g.account_id = ?"
        " ORDER BY g.created_at DESC, g.id",
        (account_id,),
    )
    return [dict(r) for r in rows]


def delete_grant(conn: sqlite3.Connection, account_id: str, grant_id: str) -> list[str] | None:
    """End one of the account's connections; returns the token ids to revoke, or None."""
    row = conn.execute(
        "SELECT token_ids FROM mcp_grant WHERE id = ? AND account_id = ?",
        (grant_id[:40], account_id),
    ).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM mcp_grant WHERE id = ?", (grant_id[:40],))
    return json.loads(row["token_ids"])


def drop_grant(conn: sqlite3.Connection, grant_id: str) -> None:
    """Delete a connection outright (its tokens are revoked by the caller)."""
    conn.execute("DELETE FROM mcp_grant WHERE id = ?", (grant_id,))
