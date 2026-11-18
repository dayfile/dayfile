import re

from dayfile.ulid import new_ulid

PATTERN = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def test_format_matches_schema_pattern():
    assert PATTERN.match(new_ulid())


def test_time_prefix_is_sortable():
    earlier = new_ulid(now_ms=1_000_000_000_000)
    later = new_ulid(now_ms=1_000_000_000_001)
    assert earlier[:10] < later[:10]


def test_known_timestamp_encoding():
    # 48-bit ms timestamp 0 encodes to ten zeros.
    assert new_ulid(now_ms=0)[:10] == "0000000000"


def test_ids_are_unique():
    assert len({new_ulid() for _ in range(1000)}) == 1000
