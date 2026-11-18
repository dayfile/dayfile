"""ULID generation (Crockford base32, 48-bit ms timestamp + 80 random bits)."""

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(now_ms: int | None = None) -> str:
    """Return a new 26-character ULID, using the current time unless `now_ms` is given."""
    ms = time.time_ns() // 1_000_000 if now_ms is None else now_ms
    value = (ms << 80) | secrets.randbits(80)
    return "".join(_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))
