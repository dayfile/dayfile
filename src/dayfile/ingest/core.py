"""Transport-independent ingest: normalize, default, validate, insert atomically."""

import copy
import sqlite3
from dataclasses import dataclass
from typing import Any

from dayfile import db, signing
from dayfile.auth.tokens import Principal
from dayfile.models import EventValidationError, validate_event
from dayfile.timefmt import normalize
from dayfile.ulid import clean_id_tail

TIME_FIELDS = ("occurred_at", "visible_at", "expires_at")


class IngestError(ValueError):
    """Raised when one or more events are malformed; nothing is written."""


class ConflictError(Exception):
    """Raised when an event id is already taken by a different source reference."""


class ForbiddenError(Exception):
    """Raised when a writer's credential does not permit an event."""


@dataclass
class IngestResult:
    """A stored event and whether this call created it."""

    event: dict[str, Any]
    created: bool


def prepare(event: Any) -> dict[str, Any]:
    """Return a normalized, defaulted, validated copy of an incoming event."""
    if not isinstance(event, dict):
        raise IngestError("event must be a JSON object")
    ev = copy.deepcopy(event)
    for field in TIME_FIELDS:
        if isinstance(ev.get(field), str):
            try:
                ev[field] = normalize(ev[field])
            except ValueError as e:
                raise IngestError(f"{field}: {e}") from None
    if ev.get("audience") == []:
        del ev["audience"]
    try:
        validate_event(ev)
    except EventValidationError as e:
        raise IngestError(str(e)) from None
    ev.setdefault("visible_at", ev["occurred_at"])
    ev.setdefault("audience", ["self"])
    if "refs" in ev:
        ev["refs"] = list({(r["rel"], r["id"]): r for r in ev["refs"]}.values())
    return ev


def ingest_events(
    conn: sqlite3.Connection, events: list[Any], writer: Principal | None = None
) -> list[IngestResult]:
    """Store events in one transaction; a re-delivered (source.id, source.ref) is a no-op.

    With a `writer`, every event must use one of the writer's source ids and an audience inside the
    writer's labels, else ForbiddenError (nothing is written).
    """
    prepared, errors = [], []
    for i, event in enumerate(events):
        try:
            ev = prepare(_inherit_title(conn, expand_short_refs(conn, event)))
            _check_signature(conn, ev)
            prepared.append(ev)
        except IngestError as e:
            errors.append(f"event {i}: {e}")
    if errors:
        raise IngestError("; ".join(errors))
    if writer is not None:
        _authorize(writer, prepared)
    conn.execute("BEGIN IMMEDIATE")
    try:
        results = [_store(conn, ev) for ev in prepared]
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return results


CLOSING = ("completes", "supersedes", "retracts")


def expand_short_refs(conn: sqlite3.Connection, event: Any) -> Any:
    """Replace the last 6 to 25 characters of an id in a closing ref with the full event id.

    Only events sharing an audience label with the new event are searched, so a short id never
    reveals an event the writer could not see. An id that matches no event or more than one is an
    IngestError. Signed events are never rewritten, so a short id in one fails validation.
    """
    if not isinstance(event, dict) or "sig" in event or not isinstance(event.get("refs"), list):
        return event
    labels = event.get("audience") or ["self"]
    if not isinstance(labels, list):
        return event
    refs = []
    for ref in event["refs"]:
        raw = ref.get("id") if isinstance(ref, dict) and ref.get("rel") in CLOSING else None
        tail = clean_id_tail(raw) if isinstance(raw, str) else None
        if tail is not None:
            ref = {**ref, "id": _full_id(conn, raw, tail, [str(x) for x in labels])}
        refs.append(ref)
    return {**event, "refs": refs}


def _full_id(conn: sqlite3.Connection, raw: str, tail: str, labels: list[str]) -> str:
    rows = conn.execute(
        "SELECT e.id FROM events e WHERE substr(e.id, -6) = ? AND substr(e.id, -?) = ?"
        " AND EXISTS (SELECT 1 FROM event_audience a WHERE a.seq = e.seq"
        f" AND a.label IN ({', '.join('?' for _ in labels)})) LIMIT 2",
        [tail[-6:], len(tail), tail, *labels],
    ).fetchall()
    if not rows:
        raise IngestError(f"no event ends in {raw!r}")
    if len(rows) > 1:
        raise IngestError(f"more than one event ends in {raw!r}; give more characters")
    return rows[0]["id"]


def _inherit_title(conn: sqlite3.Connection, event: Any) -> Any:
    """Give an untitled closing event (completes, supersedes, retracts) the title of its target.

    Only a target that shares an audience label with the new event is used, so a title is never
    learned from an event the writer could not see. Signed events are never completed.
    """
    if not isinstance(event, dict) or event.get("title") or "sig" in event:
        return event
    labels = event.get("audience") or ["self"]
    refs = event.get("refs")
    if not isinstance(refs, list) or not isinstance(labels, list):
        return event
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("rel") not in CLOSING:
            continue
        row = conn.execute(
            "SELECT e.title FROM events e WHERE e.id = ? AND EXISTS (SELECT 1 FROM event_audience a"
            f" WHERE a.seq = e.seq AND a.label IN ({', '.join('?' for _ in labels)}))",
            [str(ref.get("id"))] + [str(x) for x in labels],
        ).fetchone()
        if row:
            return {**event, "title": row["title"]}
    return event


def _authorize(writer: Principal, events: list[dict[str, Any]]) -> None:
    """Bind source and audience to the credential; the body cannot widen either."""
    for i, ev in enumerate(events):
        if ev["source"]["id"] not in writer.writer_sources:
            raise ForbiddenError(f"event {i}: source.id is not permitted for this credential")
        extra = set(ev["audience"]) - writer.labels
        if extra:
            raise ForbiddenError(f"event {i}: audience outside the credential's labels")


def _check_signature(conn: sqlite3.Connection, ev: dict[str, Any]) -> None:
    """Reject an event whose signature fails against a key we trust; unknown keys pass."""
    if "sig" not in ev:
        return
    public = signing.public_key_for(conn, ev["sig"]["kid"])
    if public is not None and not signing.verify_event(ev, public):
        raise IngestError(f"signature does not verify (kid {ev['sig']['kid']})")


def _store(conn: sqlite3.Connection, ev: dict[str, Any]) -> IngestResult:
    ref = ev["source"].get("ref")
    if ref is not None and (seq := db.find_by_source(conn, ev["source"]["id"], ref)):
        return IngestResult(db.get_event(conn, seq), created=False)
    try:
        seq = db.insert_event(conn, ev)
    except sqlite3.IntegrityError as e:
        if "events.id" in str(e):
            raise ConflictError(f"event id {ev['id']} already exists") from None
        raise
    return IngestResult(db.get_event(conn, seq), created=True)
