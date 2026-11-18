"""Email-in adapter: turn IMAP messages (including Google Voice SMS forwards) into events."""

import email
import email.policy
import email.utils
import hashlib
import imaplib
import logging
import os
import re
import select
import sqlite3
import time
from datetime import UTC
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from dayfile.ingest.core import IngestError, ingest_events
from dayfile.timefmt import normalize, now
from dayfile.ulid import new_ulid

log = logging.getLogger(__name__)

SOURCE_ID = "adapter:email-in"
VOICE_DOMAIN = "txt.voice.google.com"
TITLE_MAX, BODY_MAX = 300, 20000
TODO_PREFIX = re.compile(r"^\s*todo:\s*", re.IGNORECASE)
VOICE_SUBJECT = re.compile(
    r"^New text message from (?P<name>.*?)\s*(?P<number>\(?\+?[\d\s()-]{7,})$"
)


def message_to_event(raw: bytes) -> dict[str, Any]:
    """Convert a raw RFC 5322 message into an event envelope."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    subject = " ".join(str(msg["Subject"] or "").split())
    sender = email.utils.parseaddr(str(msg["From"] or ""))[1]
    is_voice = sender.lower().endswith("@" + VOICE_DOMAIN)

    if is_voice:
        text, payload, tags = _voice_text(_plain_body(msg)), _voice_payload(subject), ["sms"]
    else:
        text, payload, tags = _plain_body(msg), None, []
    headline = text.strip() if is_voice else subject
    is_todo = bool(TODO_PREFIX.match(headline))
    headline = TODO_PREFIX.sub("", headline, count=1)
    title = next((ln.strip() for ln in headline.splitlines() if ln.strip()), "")
    title = (title or subject or "(no subject)")[:TITLE_MAX]
    body = text.strip()
    if is_voice and is_todo:
        body = TODO_PREFIX.sub("", body, count=1)

    ev: dict[str, Any] = {
        "specversion": "dayfile/1",
        "id": new_ulid(),
        "source": {"id": SOURCE_ID, "ref": _source_ref(msg, raw)},
        "occurred_at": _occurred_at(msg),
        "title": title,
    }
    if body:
        ev["body"] = body[:BODY_MAX]
    if is_todo:
        tags = [*tags, "todo"]
    if tags:
        ev["tags"] = tags
    if payload:
        ev["payload"] = payload
    return ev


def _source_ref(msg: EmailMessage, raw: bytes) -> str:
    message_id = str(msg["Message-ID"] or "").strip()
    return hashlib.sha256(message_id.encode() if message_id else raw).hexdigest()


def _occurred_at(msg: EmailMessage) -> str:
    try:
        dt = email.utils.parsedate_to_datetime(str(msg["Date"]))
        return normalize(dt.replace(tzinfo=dt.tzinfo or UTC).isoformat())
    except (TypeError, ValueError):
        return now()


def _plain_body(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain",))
    return part.get_content() if part is not None else ""


def _voice_text(body: str) -> str:
    """Strip the Google Voice link header and footer, leaving the SMS text."""
    lines = []
    for line in body.splitlines():
        if line.startswith("YOUR ACCOUNT"):
            break
        lines.append(line)
    while lines and (not lines[0].strip() or lines[0].strip() == "<https://voice.google.com>"):
        lines.pop(0)
    return "\n".join(lines).strip()


def _voice_payload(subject: str) -> dict[str, str] | None:
    m = VOICE_SUBJECT.match(subject)
    if not m:
        return None
    return {"from_name": m["name"], "from_number": m["number"].strip()}


def poll_once(imap: Any, conn: sqlite3.Connection, folder: str, last_uid: int) -> int:
    """Ingest messages newer than `last_uid` from a folder (read-only); return the new cursor."""
    imap.select(folder, readonly=True)
    _, data = imap.uid("search", None, f"UID {last_uid + 1}:*")
    uids = sorted(int(u) for u in data[0].split() if int(u) > last_uid)
    for uid in uids:
        _, parts = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
        try:
            ingest_events(conn, [message_to_event(parts[0][1])])
        except IngestError as e:
            log.warning("skipping uid %s: %s", uid, e)
        last_uid = uid
    return last_uid


def credentials(env_file: str | Path = ".env") -> tuple[str, str]:
    """Read GMAIL_USER and GMAIL_APP_PASSWORD from the environment, falling back to .env."""
    values = {k: os.environ[k] for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD") if k in os.environ}
    path = Path(env_file)
    if len(values) < 2 and path.exists():
        for line in path.read_text().splitlines():
            key, sep, val = line.partition("=")
            if sep and key.strip() in ("GMAIL_USER", "GMAIL_APP_PASSWORD"):
                values.setdefault(key.strip(), val.strip().strip("\"'"))
    return values["GMAIL_USER"], values["GMAIL_APP_PASSWORD"].replace(" ", "")


def load_cursor(conn: sqlite3.Connection, folder: str) -> tuple[str | None, int]:
    """Return the saved (UIDVALIDITY, last UID) for a folder, or (None, 0) if never synced."""
    row = conn.execute("SELECT value FROM adapter_state WHERE key = ?", (_key(folder),)).fetchone()
    if row is None:
        return None, 0
    validity, _, uid = row["value"].partition(":")
    return validity or None, int(uid)


def save_cursor(conn: sqlite3.Connection, folder: str, validity: str | None, uid: int) -> None:
    """Persist the folder's UIDVALIDITY and last ingested UID."""
    conn.execute(
        "INSERT INTO adapter_state (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (_key(folder), f"{validity or ''}:{uid}"),
    )


def _key(folder: str) -> str:
    return f"email-in:{folder}"


def _validity(imap: Any) -> str | None:
    """UIDVALIDITY of the selected folder, or None if the server (or a test double) has none."""
    getter = getattr(imap, "response", None)
    if getter is None:
        return None
    _, data = getter("UIDVALIDITY")
    return data[0].decode() if data and data[0] else None


def sync(imap: Any, conn: sqlite3.Connection, folder: str) -> int:
    """Ingest new messages and persist the cursor; a changed UIDVALIDITY restarts from UID 0.

    Restarting is safe because ingest is idempotent on the Message-ID hash.
    """
    imap.select(folder, readonly=True)
    validity = _validity(imap)
    saved_validity, last_uid = load_cursor(conn, folder)
    if saved_validity != validity and saved_validity is not None:
        log.warning("UIDVALIDITY of %s changed; rescanning from the start", folder)
        last_uid = 0
    last_uid = poll_once(imap, conn, folder, last_uid)
    save_cursor(conn, folder, validity, last_uid)
    return last_uid


def idle_wait(imap: Any, timeout: float) -> bool:
    """Block in IMAP IDLE until the mailbox changes or `timeout` passes; True if it changed.

    imaplib has no IDLE before Python 3.14, so this speaks the protocol over its socket.
    A refused IDLE raises imaplib.IMAP4.error; the caller falls back to polling.
    """
    tag = imap._new_tag()
    imap.send(tag + b" IDLE\r\n")
    first = imap.readline()
    if not first.startswith(b"+"):
        raise imaplib.IMAP4.error(f"IDLE refused: {first.decode(errors='replace').strip()}")
    changed = False
    deadline = time.monotonic() + timeout
    while not changed and (left := deadline - time.monotonic()) > 0:
        if not select.select([imap.sock], [], [], left)[0]:
            break
        changed = _announces_change(imap.readline())
    imap.send(b"DONE\r\n")
    while line := imap.readline():  # lines we already buffered also count
        changed = changed or _announces_change(line)
        if line.startswith(tag):
            break
    return changed


def _announces_change(line: bytes) -> bool:
    return line.startswith(b"* ") and (b" EXISTS" in line or b" EXPUNGE" in line)


def run(
    conn: sqlite3.Connection,
    folder: str,
    interval: float,
    once: bool = False,
    idle_timeout: float | None = 540,
) -> None:
    """Keep a Gmail folder synced, forever or once, reconnecting after errors.

    With `idle_timeout` set, waits in IMAP IDLE (Gmail drops idle sessions after about 29
    minutes, so re-enter well before that) and syncs when the folder changes or the wait
    ends. With None, or if the server refuses IDLE, sleeps `interval` between syncs.
    """
    user, password = credentials()
    while True:
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com")
            try:
                imap.login(user, password)
                while True:
                    sync(imap, conn, folder)
                    if once:
                        return
                    if idle_timeout is None:
                        time.sleep(interval)
                        continue
                    try:
                        idle_wait(imap, idle_timeout)
                    except imaplib.IMAP4.error as e:
                        log.warning("IDLE unavailable (%s); polling every %ss", e, interval)
                        idle_timeout = None
            finally:
                try:
                    imap.logout()
                except (imaplib.IMAP4.error, OSError):
                    pass
        except (imaplib.IMAP4.error, OSError) as e:
            log.warning("sync failed: %s", e)
        if once:
            return
        time.sleep(interval)
