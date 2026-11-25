"""ULID generation (Crockford base32, 48-bit ms timestamp + 80 random bits)."""

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(now_ms: int | None = None) -> str:
    """Return a new 26-character ULID, using the current time unless `now_ms` is given."""
    ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    value = (ms << 80) | secrets.randbits(80)
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))


MIN_TAIL = 6


def clean_id_tail(text: str) -> str | None:
    """Normalize the last `MIN_TAIL` to 25 characters of an event id typed by a person.

    Case is ignored and Crockford's look-alikes are mapped (O to 0, I and L to 1). Returns None
    if the text is not a valid tail.
    """
    tail = text.strip().upper().translate(str.maketrans("OIL", "011"))
    if not MIN_TAIL <= len(tail) < 26 or any(c not in _ALPHABET for c in tail):
        return None
    return tail
