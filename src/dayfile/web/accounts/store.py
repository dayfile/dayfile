"""The accounts database: accounts, invite codes and an append-only audit log (stdlib sqlite3)."""

import hashlib
import json
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from dayfile.tenancy import RESERVED, SLUG
from dayfile.timefmt import minus, plus
from dayfile.ulid import new_ulid

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O or U
CODE_CHARS = 12
MAX_CODE_INPUT = 64

MIGRATIONS = [
    """
    CREATE TABLE account (
      id           TEXT PRIMARY KEY,
      github_id    INTEGER NOT NULL UNIQUE,
      github_login TEXT NOT NULL,
      created_at   TEXT NOT NULL,
      status       TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','suspended','deleted')),
      slug         TEXT UNIQUE,
      terms_version TEXT,
      terms_accepted_at TEXT,
      deleted_at   TEXT
    );
    CREATE TABLE invite (
      id         TEXT PRIMARY KEY,
      code_hash  BLOB NOT NULL UNIQUE,
      note       TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      expires_at TEXT,
      max_uses   INTEGER NOT NULL CHECK (max_uses >= 1),
      uses       INTEGER NOT NULL DEFAULT 0 CHECK (uses >= 0),
      revoked_at TEXT
    );
    CREATE TABLE redeem (
      invite_id  TEXT NOT NULL REFERENCES invite(id),
      account_id TEXT NOT NULL REFERENCES account(id),
      at         TEXT NOT NULL
    );
    CREATE TABLE audit (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      at         TEXT NOT NULL,
      account_id TEXT,
      action     TEXT NOT NULL,
      detail     TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
      BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
    CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
      BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
    """,
    """
    CREATE TABLE session (
      id_hash      BLOB PRIMARY KEY,
      account_id   TEXT REFERENCES account(id),
      github_id    INTEGER,
      github_login TEXT,
      created_at   TEXT NOT NULL,
      last_seen    TEXT NOT NULL,
      expires_at   TEXT NOT NULL
    );
    CREATE TABLE oauth (
      state_hash BLOB PRIMARY KEY,
      verifier   TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE slug_hold (
      slug       TEXT PRIMARY KEY,
      account_id TEXT NOT NULL REFERENCES account(id),
      until      TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE mcp_client (
      id            TEXT PRIMARY KEY,
      name          TEXT NOT NULL,
      redirect_uris TEXT NOT NULL,
      created_at    TEXT NOT NULL
    );
    CREATE TABLE mcp_code (
      code_hash    BLOB PRIMARY KEY,
      client_id    TEXT NOT NULL REFERENCES mcp_client(id),
      account_id   TEXT NOT NULL REFERENCES account(id),
      redirect_uri TEXT NOT NULL,
      challenge    TEXT NOT NULL,
      can_write    INTEGER NOT NULL,
      expires_at   TEXT NOT NULL
    );
    CREATE TABLE mcp_grant (
      id                 TEXT PRIMARY KEY,
      client_id          TEXT NOT NULL REFERENCES mcp_client(id),
      account_id         TEXT NOT NULL REFERENCES account(id),
      can_write          INTEGER NOT NULL,
      created_at         TEXT NOT NULL,
      last_used          TEXT NOT NULL,
      refresh_hash       BLOB NOT NULL UNIQUE,
      prev_hash          BLOB,
      refresh_expires_at TEXT NOT NULL,
      token_ids          TEXT NOT NULL DEFAULT '[]'
    );
    CREATE INDEX mcp_grant_account ON mcp_grant (account_id);
    """,
    """
    UPDATE account SET github_id = -rowid, github_login = 'deleted'
      WHERE status = 'deleted' AND github_id > 0;
    """,
    """
    ALTER TABLE slug_hold ADD COLUMN github_id INTEGER;
    """,
    """
    CREATE TABLE signup_answer (
      account_id  TEXT PRIMARY KEY REFERENCES account(id),
      text        TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 500),
      answered_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE contact_email (
      account_id      TEXT PRIMARY KEY REFERENCES account(id),
      email           TEXT NOT NULL CHECK (length(email) BETWEEN 3 AND 254),
      consent_version TEXT NOT NULL,
      consented_at    TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE feature_interest (
      id              INTEGER PRIMARY KEY,
      feature         TEXT NOT NULL,
      account_id      TEXT REFERENCES account(id),
      email           TEXT CHECK (email IS NULL OR length(email) BETWEEN 3 AND 254),
      consent_version TEXT,
      created_at      TEXT NOT NULL
    );
    CREATE UNIQUE INDEX feature_interest_one ON feature_interest (account_id, feature)
      WHERE account_id IS NOT NULL;
    """,
]
HOLD_SECONDS = 86400  # a name just given up stays its owner's for one day
RENAMES_PER_MONTH = 3
IDLE_SECONDS = 8 * 3600
ABSOLUTE_SECONDS = 30 * 86400
OAUTH_SECONDS = 600
# Names nobody may take: the platform's own words, mail and infrastructure names, and look-alikes.
RESERVED_SLUGS = RESERVED | frozenset(
    "app admin api cdn dashboard docs help support team security abuse postmaster root dayfile "
    "internal test demo dogfood static assets login signup auth account accounts billing "
    "blog status mail smtp imap ftp ns ns1 ns2 ns3 www".split()
)
TERMS_VERSION = "2026-11-draft-1"
MAX_ANSWER = 500
# Planned features people can ask to hear about (none exists yet); the words are shown on /pricing.
FEATURES = {
    "alerts": "Alerts when something doesn't run",
    "sharing": "Share a view with a client",
    "teams": "Team logs",
}
# The exact words a user agrees to; bump the version whenever they change.
EMAIL_CONSENT = (
    "The developer may email me about dayfile: service changes and one message"
    " asking how it's going."
)
EMAIL_CONSENT_VERSION = "2026-11-email-1"
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s]+\.[^@\s.]+")
# What a plain-text answer may not keep: C0/C1 controls (but tab and newline), bidi overrides.
_UNPRINTABLE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


class InviteError(ValueError):
    """The invite code is unknown, used up, expired or revoked (deliberately not distinguished)."""


class AccountExists(ValueError):
    """This GitHub identity already has an account."""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating and migrating if needed) the accounts database, private to its owner."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    if fresh:
        path.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.execute("BEGIN IMMEDIATE")
        for statement in _statements(script):
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {number}")
        conn.execute("COMMIT")
    return conn


def _statements(script: str) -> list[str]:
    """Split a migration into statements, keeping CREATE TRIGGER bodies whole."""
    out, current = [], ""
    for line in script.splitlines():
        current += line + "\n"
        if sqlite3.complete_statement(current):
            out.append(current.strip())
            current = ""
    return out


def _normalize(code: str) -> str:
    """Canonical form of a typed code: upper case, no separators, O to 0 and I/L to 1."""
    text = code.strip().upper()[:MAX_CODE_INPUT]
    text = "".join(ch for ch in text if ch not in " -")
    return text.translate(str.maketrans("OIL", "011"))


def _hash(code: str) -> bytes:
    return hashlib.sha256(_normalize(code).encode()).digest()


def _audit(conn: sqlite3.Connection, now: str, action: str, account_id: str | None, **detail: Any):
    conn.execute(
        "INSERT INTO audit (at, account_id, action, detail) VALUES (?, ?, ?, ?)",
        (now, account_id, action, json.dumps(detail, sort_keys=True)),
    )


def create_invite(
    conn: sqlite3.Connection,
    note: str,
    now: str,
    uses: int = 1,
    expires_at: str | None = None,
) -> str:
    """Make an invite and return its code, which is shown once and stored only as a hash."""
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_CHARS))
    code = "-".join(raw[i : i + 4] for i in range(0, CODE_CHARS, 4))
    invite_id = new_ulid()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO invite (id, code_hash, note, created_at, expires_at, max_uses)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (invite_id, _hash(code), note, now, expires_at, uses),
    )
    _audit(conn, now, "invite.create", None, invite=invite_id, note=note, uses=uses)
    conn.execute("COMMIT")
    return code


def list_invites(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every invite in creation order, without any code material."""
    rows = conn.execute(
        "SELECT id, note, created_at, expires_at, max_uses, uses, revoked_at FROM invite"
        " ORDER BY rowid"
    )
    return [dict(r) for r in rows]


def revoke_invite(conn: sqlite3.Connection, invite_id: str, now: str) -> bool:
    """Revoke an invite; return False if it does not exist or was already revoked."""
    conn.execute("BEGIN IMMEDIATE")
    changed = conn.execute(
        "UPDATE invite SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (now, invite_id)
    ).rowcount
    if changed:
        _audit(conn, now, "invite.revoke", None, invite=invite_id)
    conn.execute("COMMIT")
    return bool(changed)


def create_account(
    conn: sqlite3.Connection, code: str | None, github_id: int, github_login: str, now: str
) -> dict[str, Any]:
    """Create the account in one transaction, redeeming `code` unless it is None (open signup)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM account WHERE github_id = ?", (github_id,)).fetchone():
            raise AccountExists("this GitHub account is already registered")
        invite = None
        if code is not None:
            invite = conn.execute(
                "SELECT id FROM invite WHERE code_hash = ? AND revoked_at IS NULL"
                " AND uses < max_uses AND (expires_at IS NULL OR expires_at > ?)",
                (_hash(code), now),
            ).fetchone()
            if invite is None:
                raise InviteError("that invite code is not valid")
            conn.execute("UPDATE invite SET uses = uses + 1 WHERE id = ?", (invite["id"],))
        account_id = new_ulid()
        conn.execute(
            "INSERT INTO account (id, github_id, github_login, created_at) VALUES (?, ?, ?, ?)",
            (account_id, github_id, github_login, now),
        )
        if invite is not None:
            conn.execute(
                "INSERT INTO redeem (invite_id, account_id, at) VALUES (?, ?, ?)",
                (invite["id"], account_id, now),
            )
        _audit(
            conn,
            now,
            "account.create",
            account_id,
            invite=invite["id"] if invite else None,
            login=github_login,
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return get_account(conn, github_id=github_id) or {}


def get_account(conn: sqlite3.Connection, github_id: int | None = None) -> dict[str, Any] | None:
    """Look an account up by its numeric GitHub id."""
    row = conn.execute("SELECT * FROM account WHERE github_id = ?", (github_id,)).fetchone()
    return dict(row) if row else None


def get_account_by_id(conn: sqlite3.Connection, account_id: str) -> dict[str, Any] | None:
    """Look an account up by its id."""
    row = conn.execute("SELECT * FROM account WHERE id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


def list_accounts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every account in creation order."""
    return [dict(r) for r in conn.execute("SELECT * FROM account ORDER BY rowid")]


def account_stats(
    conn: sqlite3.Connection, now: str, exclude: frozenset[str] = frozenset(), days: int = 30
) -> dict[str, Any]:
    """Count accounts by status and sign-ups per UTC day for the last `days` days; numbers only."""
    rows = [
        r
        for r in conn.execute("SELECT status, slug, created_at FROM account")
        if r["slug"] not in exclude
    ]
    by_status = {s: 0 for s in ("active", "suspended", "deleted")}
    for r in rows:
        by_status[r["status"]] += 1
    per_day = {minus(now, d * 86400)[:10]: 0 for d in range(days - 1, -1, -1)}
    for r in rows:
        if r["created_at"][:10] in per_day:
            per_day[r["created_at"][:10]] += 1
    answered = conn.execute(
        "SELECT a.slug FROM signup_answer s JOIN account a ON a.id = s.account_id"
    ).fetchall()
    with_answer = sum(r["slug"] not in exclude for r in answered)
    emailed = conn.execute(
        "SELECT a.slug FROM contact_email e JOIN account a ON a.id = e.account_id"
        " WHERE a.status = 'active'"
    ).fetchall()
    interest = {f: {"signed_in": 0, "signed_out": 0} for f in FEATURES}
    for r in conn.execute(
        "SELECT i.feature, i.account_id, a.slug FROM feature_interest i"
        " LEFT JOIN account a ON a.id = i.account_id"
    ):
        if r["feature"] in interest and r["slug"] not in exclude:
            interest[r["feature"]]["signed_in" if r["account_id"] else "signed_out"] += 1
    return {
        "at": now,
        "feature_interest": interest,
        "accounts": by_status,
        "new_per_day": per_day,
        "with_signup_answer": with_answer,
        "with_contact_email": sum(r["slug"] not in exclude for r in emailed),
    }


def clean_answer(text: str) -> str:
    """Plain text for a sign-up answer: one kind of newline, no control characters, trimmed."""
    return _UNPRINTABLE.sub("", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def set_answer(conn: sqlite3.Connection, account_id: str, text: str, now: str) -> bool:
    """Store the account's answer, or remove it when blank; True if one is now stored."""
    text = clean_answer(text)
    if len(text) > MAX_ANSWER:
        raise ValueError(f"Keep it under {MAX_ANSWER} characters.")
    if not text:
        conn.execute("DELETE FROM signup_answer WHERE account_id = ?", (account_id,))
        return False
    conn.execute(
        "INSERT INTO signup_answer (account_id, text, answered_at) VALUES (?, ?, ?)"
        " ON CONFLICT (account_id) DO UPDATE SET text = excluded.text,"
        " answered_at = excluded.answered_at",
        (account_id, text, now),
    )
    return True


def get_answer(conn: sqlite3.Connection, account_id: str) -> dict[str, Any] | None:
    """Return the account's sign-up answer as {text, answered_at}, or None."""
    row = conn.execute(
        "SELECT text, answered_at FROM signup_answer WHERE account_id = ?", (account_id,)
    ).fetchone()
    return dict(row) if row else None


def list_answers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every sign-up answer, oldest first, with no account attached."""
    return [
        dict(r)
        for r in conn.execute("SELECT text, answered_at FROM signup_answer ORDER BY answered_at")
    ]


def clean_email(text: str) -> str:
    """Return a plausible email address (domain lower-cased), or raise ValueError."""
    local, _, domain = text.strip().rpartition("@")
    email = f"{local}@{domain.lower()}"
    if len(email) > 254 or not _EMAIL.fullmatch(email) or _UNPRINTABLE.search(email):
        raise ValueError("That doesn't look like an email address.")
    return email


def set_email(conn: sqlite3.Connection, account_id: str, email: str, now: str) -> None:
    """Store a contact address with consent to the current wording, given now."""
    conn.execute(
        "INSERT INTO contact_email (account_id, email, consent_version, consented_at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT (account_id) DO UPDATE SET email = excluded.email,"
        " consent_version = excluded.consent_version, consented_at = excluded.consented_at",
        (account_id, clean_email(email), EMAIL_CONSENT_VERSION, now),
    )


def remove_email(conn: sqlite3.Connection, account_id: str) -> None:
    """Forget the contact address; consent is withdrawn with it."""
    conn.execute("DELETE FROM contact_email WHERE account_id = ?", (account_id,))


def get_email(conn: sqlite3.Connection, account_id: str) -> dict[str, Any] | None:
    """Return {email, consent_version, consented_at} for the account, or None."""
    row = conn.execute(
        "SELECT email, consent_version, consented_at FROM contact_email WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    return dict(row) if row else None


def list_emails(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every consenting address of a live account, oldest consent first."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT e.email, e.consent_version, e.consented_at FROM contact_email e"
            " JOIN account a ON a.id = e.account_id WHERE a.status = 'active'"
            " ORDER BY e.consented_at"
        )
    ]


def set_interests(conn: sqlite3.Connection, account_id: str, features: list[str], now: str) -> None:
    """Make the account's roadmap interest exactly `features`; unchanged ones keep their time."""
    chosen = {f for f in features if f in FEATURES}
    have = my_interests(conn, account_id)
    for feature in have - chosen:
        conn.execute(
            "DELETE FROM feature_interest WHERE account_id = ? AND feature = ?",
            (account_id, feature),
        )
    for feature in sorted(chosen - have):
        conn.execute(
            "INSERT INTO feature_interest (feature, account_id, created_at) VALUES (?, ?, ?)",
            (feature, account_id, now),
        )


def add_interest(conn: sqlite3.Connection, feature: str, email: str | None, now: str) -> None:
    """Record a signed-out visitor's interest; an address is kept only if given with consent."""
    if feature not in FEATURES:
        raise ValueError("unknown feature")
    address = clean_email(email) if email else None
    if (
        address
        and conn.execute(
            "SELECT 1 FROM feature_interest WHERE feature = ? AND email = ?", (feature, address)
        ).fetchone()
    ):
        return  # the same address asking twice is one request
    conn.execute(
        "INSERT INTO feature_interest (feature, email, consent_version, created_at)"
        " VALUES (?, ?, ?, ?)",
        (feature, address, EMAIL_CONSENT_VERSION if address else None, now),
    )


def my_interests(conn: sqlite3.Connection, account_id: str) -> set[str]:
    """The planned features this account has asked to hear about."""
    rows = conn.execute("SELECT feature FROM feature_interest WHERE account_id = ?", (account_id,))
    return {r["feature"] for r in rows}


def interest_emails(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Addresses that signed-out visitors left with consent, oldest first."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT feature, email, consent_version, created_at FROM feature_interest"
            " WHERE email IS NOT NULL ORDER BY created_at"
        )
    ]


def account_export(conn: sqlite3.Connection, account_id: str) -> dict[str, Any]:
    """Everything the accounts database holds about one account, for its owner to download."""
    row = conn.execute(
        "SELECT github_id, github_login, slug, created_at, terms_version, terms_accepted_at"
        " FROM account WHERE id = ?",
        (account_id,),
    ).fetchone()
    return {
        "account": dict(row) if row else None,
        "signup_answer": get_answer(conn, account_id),
        "contact_email": get_email(conn, account_id),
        "feature_interest": [
            dict(r)
            for r in conn.execute(
                "SELECT feature, created_at FROM feature_interest WHERE account_id = ?"
                " ORDER BY created_at",
                (account_id,),
            )
        ],
    }


def set_status(conn: sqlite3.Connection, login: str, status: str, now: str) -> bool:
    """Suspend or reactivate the account with this GitHub login; False if there is none."""
    if status not in ("active", "suspended"):
        raise ValueError(f"cannot set status {status!r}")
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT id FROM account WHERE github_login = ?", (login,)).fetchone()
    if row:
        conn.execute("UPDATE account SET status = ? WHERE id = ?", (status, row["id"]))
        if status == "suspended":
            conn.execute("DELETE FROM session WHERE account_id = ?", (row["id"],))
        _audit(conn, now, "account.status", row["id"], status=status)
    conn.execute("COMMIT")
    return row is not None


def _digest(raw: str) -> bytes:
    return hashlib.sha256(raw.encode()).digest()


def begin_oauth(conn: sqlite3.Connection, now: str) -> tuple[str, str]:
    """Start a GitHub sign-in: return (state, PKCE verifier); only the state's hash is stored."""
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    conn.execute("DELETE FROM oauth WHERE created_at <= ?", (minus(now, OAUTH_SECONDS),))
    conn.execute(
        "INSERT INTO oauth (state_hash, verifier, created_at) VALUES (?, ?, ?)",
        (_digest(state), verifier, now),
    )
    return state, verifier


def finish_oauth(conn: sqlite3.Connection, state: str, now: str) -> str | None:
    """Use up a state once, within ten minutes; return its verifier or None."""
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT verifier FROM oauth WHERE state_hash = ? AND created_at > ?",
        (_digest(state), minus(now, OAUTH_SECONDS)),
    ).fetchone()
    conn.execute("DELETE FROM oauth WHERE state_hash = ?", (_digest(state),))
    conn.execute("COMMIT")
    return row["verifier"] if row else None


def create_session(
    conn: sqlite3.Connection,
    now: str,
    account_id: str | None = None,
    github: tuple[int, str] | None = None,
) -> str:
    """Make a session for an account, or a pending one for a GitHub identity without an account."""
    if (account_id is None) == (github is None):
        raise ValueError("a session belongs to an account or to a pending GitHub identity")
    raw = secrets.token_urlsafe(32)
    gid, login = github or (None, None)
    conn.execute(
        "INSERT INTO session (id_hash, account_id, github_id, github_login, created_at,"
        " last_seen, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_digest(raw), account_id, gid, login, now, now, plus(now, ABSOLUTE_SECONDS)),
    )
    return raw


def get_session(conn: sqlite3.Connection, raw: str, now: str) -> dict[str, Any] | None:
    """Return a live session (idle under 8 hours, under 30 days old) and refresh its last use."""
    if not raw or len(raw) > 100:
        return None
    row = conn.execute(
        "SELECT s.*, a.status, a.github_login AS account_login, a.slug FROM session s"
        " LEFT JOIN account a ON a.id = s.account_id WHERE s.id_hash = ?",
        (_digest(raw),),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= now or row["last_seen"] <= minus(now, IDLE_SECONDS):
        conn.execute("DELETE FROM session WHERE id_hash = ?", (row["id_hash"],))
        return None
    if row["account_id"] and row["status"] != "active":
        return None
    conn.execute("UPDATE session SET last_seen = ? WHERE id_hash = ?", (now, row["id_hash"]))
    return {
        "account_id": row["account_id"],
        "login": row["account_login"] or row["github_login"],
        "github_id": row["github_id"],
        "slug": row["slug"],
        "pending": row["account_id"] is None,
    }


def end_session(conn: sqlite3.Connection, raw: str) -> None:
    """Delete a session (sign out); unknown ids are ignored."""
    conn.execute("DELETE FROM session WHERE id_hash = ?", (_digest(raw),))


def delete_account(conn: sqlite3.Connection, account_id: str, now: str) -> str | None:
    """Close an account for good: mark it deleted, free its username after the hold, end sessions.

    Returns the username it held. The row stays for the audit log, but it lets go of the GitHub
    identity (a negative placeholder id), so the same person can sign up again as a new account.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT slug, github_id FROM account WHERE id = ?", (account_id,)
        ).fetchone()
        slug = row["slug"] if row else None
        owner = row["github_id"] if row else None
        conn.execute(
            "UPDATE account SET status = 'deleted', deleted_at = ?, slug = NULL,"
            " github_id = -rowid, github_login = 'deleted' WHERE id = ?",
            (now, account_id),
        )
        if slug:
            conn.execute(
                "INSERT INTO slug_hold (slug, account_id, until, github_id) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (slug) DO UPDATE SET account_id = excluded.account_id,"
                " until = excluded.until, github_id = excluded.github_id",
                (slug, account_id, plus(now, HOLD_SECONDS), owner),
            )
        conn.execute("DELETE FROM session WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM mcp_grant WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM signup_answer WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM contact_email WHERE account_id = ?", (account_id,))
        conn.execute("DELETE FROM feature_interest WHERE account_id = ?", (account_id,))
        _audit(conn, now, "account.delete", account_id, slug=slug)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return slug


def accept_terms(conn: sqlite3.Connection, account_id: str, now: str) -> None:
    """Record that the account accepted the current terms version."""
    conn.execute(
        "UPDATE account SET terms_version = ?, terms_accepted_at = ? WHERE id = ?",
        (TERMS_VERSION, now, account_id),
    )


def refresh_login(conn: sqlite3.Connection, account_id: str, login: str) -> None:
    """Keep the displayed GitHub login current; the numeric id, not the login, is the identity."""
    conn.execute("UPDATE account SET github_login = ? WHERE id = ?", (login, account_id))


class SlugError(ValueError):
    """The requested log name is invalid, reserved or taken."""


def check_slug(slug: str) -> None:
    """Raise SlugError unless the name could be a log name (not whether it is free)."""
    if not SLUG.fullmatch(slug) or len(slug) < 3:
        raise SlugError(
            "Use 3 to 32 letters, digits or hyphens, starting and ending with a letter or digit."
        )
    if slug in RESERVED_SLUGS:
        raise SlugError("That name is reserved.")


def claim_slug(conn: sqlite3.Connection, account_id: str, slug: str, now: str) -> None:
    """Give the account its log name once; names are unique and cannot be changed later."""
    check_slug(slug)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT slug FROM account WHERE id = ?", (account_id,)).fetchone()["slug"]:
            raise SlugError("This account already has a username.")
        _refuse_if_held(conn, slug, account_id, now)
        try:
            conn.execute("UPDATE account SET slug = ? WHERE id = ?", (slug, account_id))
        except sqlite3.IntegrityError:
            raise SlugError("That name is taken.") from None
        _audit(conn, now, "account.slug", account_id, slug=slug)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def release_slug(conn: sqlite3.Connection, account_id: str, slug: str, now: str) -> None:
    """Undo a claim whose tenant could not be created."""
    conn.execute("UPDATE account SET slug = NULL WHERE id = ? AND slug = ?", (account_id, slug))
    _audit(conn, now, "account.slug_released", account_id, slug=slug)


def _refuse_if_held(conn: sqlite3.Connection, slug: str, account_id: str, now: str) -> None:
    """A name just given up is held for one day, unless the same GitHub account asks again."""
    held = conn.execute(
        "SELECT account_id, until, github_id FROM slug_hold WHERE slug = ?", (slug,)
    ).fetchone()
    if not held or held["until"] <= now or held["account_id"] == account_id:
        return
    me = conn.execute("SELECT github_id FROM account WHERE id = ?", (account_id,)).fetchone()
    if held["github_id"] is None or me is None or held["github_id"] != me["github_id"]:
        raise SlugError("That name is taken.")


def rename_slug(conn: sqlite3.Connection, account_id: str, new: str, now: str) -> str:
    """Change the account's username; return the old one. The old name is held for one day."""
    check_slug(new)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT slug FROM account WHERE id = ?", (account_id,)).fetchone()
        old = row["slug"] if row else None
        if not old:
            raise SlugError("Choose a username first.")
        if old == new:
            raise SlugError("That is already your username.")
        recent = conn.execute(
            "SELECT count(*) FROM audit WHERE account_id = ? AND action = 'account.rename'"
            " AND at > ?",
            (account_id, minus(now, 30 * 86400)),
        ).fetchone()[0]
        if recent >= RENAMES_PER_MONTH:
            raise SlugError("You have renamed too often. Try again later.")
        _refuse_if_held(conn, new, account_id, now)
        try:
            conn.execute("UPDATE account SET slug = ? WHERE id = ?", (new, account_id))
        except sqlite3.IntegrityError:
            raise SlugError("That name is taken.") from None
        conn.execute("DELETE FROM slug_hold WHERE slug = ? AND account_id = ?", (new, account_id))
        conn.execute(
            "INSERT INTO slug_hold (slug, account_id, until, github_id)"
            " VALUES (?, ?, ?, (SELECT github_id FROM account WHERE id = ?))"
            " ON CONFLICT (slug) DO UPDATE SET account_id = excluded.account_id,"
            " until = excluded.until, github_id = excluded.github_id",
            (old, account_id, plus(now, HOLD_SECONDS), account_id),
        )
        _audit(conn, now, "account.rename", account_id, old=old, new=new)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return old


def undo_rename(conn: sqlite3.Connection, account_id: str, old: str, new: str, now: str) -> None:
    """Put the old username back after the tenant move failed."""
    conn.execute("UPDATE account SET slug = ? WHERE id = ? AND slug = ?", (old, account_id, new))
    conn.execute("DELETE FROM slug_hold WHERE slug = ? AND account_id = ?", (old, account_id))
    _audit(conn, now, "account.rename_undone", account_id, old=old, new=new)
