"""Feed query parsing."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from dayfile.timefmt import normalize, parse_duration

DEFAULT_LIMIT, MAX_LIMIT = 50, 200
MULTI = {"tag": "tags", "tag_any": "tag_any", "status": "statuses"}
SCALARS = ("within", "since_seq", "limit", "released_after")
STATUSES = ("open", "closed", "superseded", "retracted", "none")
ALIASES = {"done": "closed"}  # the old name, still accepted in saved feeds and bookmarks


class QueryError(ValueError):
    """Raised for an unknown or malformed query parameter."""


@dataclass
class FeedQuery:
    """A parsed feed request. `since_seq=None` means "newest page"."""

    tags: list[str] = field(default_factory=list)
    tag_any: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    within_seconds: int | None = None
    since_seq: int | None = None
    released_after: str | None = None
    event_id: str | None = None  # exact-id lookup; ignores the default status filter
    limit: int = DEFAULT_LIMIT


def parse_query(pairs: Iterable[tuple[str, str]]) -> FeedQuery:
    """Build a FeedQuery from decoded query-string pairs, rejecting anything unknown."""
    q = FeedQuery()
    seen: set[str] = set()
    for name, value in pairs:
        if name in MULTI:
            if name == "status":
                value = ALIASES.get(value, value)
            if name == "status" and value not in STATUSES:
                raise QueryError(f"status must be one of {', '.join(STATUSES)}, got {value!r}")
            getattr(q, MULTI[name]).append(value)
        elif name in SCALARS:
            if name in seen:
                raise QueryError(f"{name} may be given only once")
            seen.add(name)
            _set_scalar(q, name, value)
        else:
            raise QueryError(f"unknown query parameter: {name}")
    if q.released_after is not None and q.since_seq is None:
        raise QueryError("released_after requires since_seq")
    return q


def _set_scalar(q: FeedQuery, name: str, value: str) -> None:
    if name == "within":
        try:
            q.within_seconds = parse_duration(value)
        except ValueError as e:
            raise QueryError(f"within: {e}") from None
        return
    if name == "released_after":
        try:
            q.released_after = normalize(value)
        except ValueError as e:
            raise QueryError(f"released_after: {e}") from None
        return
    if not re.fullmatch(r"[0-9]+", value):
        raise QueryError(f"{name} must be a non-negative integer, got {value!r}")
    number = int(value)
    if name == "since_seq":
        q.since_seq = number
    elif number < 1:
        raise QueryError("limit must be at least 1")
    else:
        q.limit = min(number, MAX_LIMIT)
