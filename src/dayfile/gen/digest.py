"""Digest generator: roll a day of events into one `digest.daily` event."""

import importlib
from collections.abc import Callable
from datetime import datetime
from typing import Any

from dayfile import db
from dayfile.ingest.core import IngestResult, ingest_events
from dayfile.timefmt import plus
from dayfile.ulid import new_ulid

SOURCE_ID = "gen:digest"
TITLE_MAX, BODY_MAX = 300, 20000
Summarizer = Callable[[str, list[dict[str, Any]]], tuple[str, str]]


def plain_summarizer(day: str, events: list[dict[str, Any]]) -> tuple[str, str]:
    """Default summarizer: a count and one line per event. An LLM can replace it."""
    n = len(events)
    title = f"Digest {day}: {n} event{'' if n == 1 else 's'}"
    lines = []
    for ev in events:
        suffix = "" if ev.get("status") in (None, "open") else f" ({ev['status']})"
        lines.append(f"- {ev['title']}{suffix}")
    return title, "\n".join(lines)


def load_summarizer(spec: str) -> Summarizer:
    """Resolve a `package.module:function` string to a summarizer."""
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ValueError(f"summarizer must look like package.module:function, got {spec!r}")
    try:
        return getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as e:
        raise ValueError(f"cannot load summarizer {spec!r}: {e}") from None


def generate(
    conn: Any,
    day: str,
    now: str,
    audience: list[str] | None = None,
    summarizer: Summarizer = plain_summarizer,
) -> IngestResult | None:
    """Write the digest for a UTC `day`, or return the existing one; None if the day is empty.

    Only events visible to `audience` are summarised, so a digest never widens access.
    """
    start = f"{datetime.strptime(day, '%Y-%m-%d'):%Y-%m-%d}T00:00:00Z"
    labels = sorted(set(audience or ["self"]))
    ref = f"{day}:{','.join(labels)}"
    if seq := db.find_by_source(conn, SOURCE_ID, ref):
        return IngestResult(db.get_event(conn, seq), created=False)
    events = db.events_between(conn, start, plus(start, 86400), now, labels)
    if not events:
        return None
    title, body = summarizer(day, events)
    event = {
        "specversion": "dayfile/1",
        "id": new_ulid(),
        "source": {"id": SOURCE_ID, "ref": ref},
        "occurred_at": now,
        "visible_at": now,
        "title": title[:TITLE_MAX],
        "body": body[:BODY_MAX],
        "tags": ["digest"],
        "payload": {"day": day, "count": len(events), "ids": [e["id"] for e in events]},
        "audience": labels,
    }
    return ingest_events(conn, [event])[0]
