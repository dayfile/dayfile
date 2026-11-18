"""UTC normalization and the one allowed timestamp format."""

import re
from datetime import UTC, datetime, timedelta

FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def normalize(value: str) -> str:
    """Convert an ISO 8601 datetime with an offset to `YYYY-MM-DDTHH:MM:SSZ` in UTC."""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"not an ISO 8601 datetime: {value!r}") from None
    if "T" not in value.upper() or dt.tzinfo is None:
        raise ValueError(f"datetime must carry a UTC offset or Z: {value!r}")
    return dt.astimezone(UTC).strftime(FORMAT)


def now() -> str:
    """Return the current time in the canonical format."""
    return datetime.now(UTC).strftime(FORMAT)


def minus(ts: str, seconds: int) -> str:
    """Return a canonical timestamp `seconds` earlier than `ts`."""
    dt = datetime.strptime(ts, FORMAT).replace(tzinfo=UTC)
    return (dt - timedelta(seconds=seconds)).strftime(FORMAT)


UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str) -> int:
    """Parse `30m`, `12h` or `7d` into seconds; raise ValueError otherwise."""
    m = re.fullmatch(r"([0-9]+)([mhd])", value)
    if not m or int(m[1]) == 0:
        raise ValueError(f"duration must look like 30m, 12h or 7d, got {value!r}")
    return int(m[1]) * UNIT_SECONDS[m[2]]


def plus(ts: str, seconds: int) -> str:
    """Return a canonical timestamp `seconds` later than `ts`."""
    return minus(ts, -seconds)
