import json
from pathlib import Path

import pytest

from dayfile.models import EventValidationError, validate_event

EXAMPLES = Path(__file__).parents[1] / "examples" / "events.jsonl"
LINES = [line for line in EXAMPLES.read_text().splitlines() if line.strip()]


def minimal():
    return {
        "specversion": "dayfile/1",
        "id": "01JC8Z4Q7M2X9K3T5W6N8R0VBA",
        "source": {"id": "test"},
        "occurred_at": "2026-11-06T13:00:00Z",
        "title": "hi",
    }


def test_example_count():
    assert len(LINES) == 8


@pytest.mark.parametrize("line", LINES)
def test_every_example_validates(line):
    validate_event(json.loads(line))


def test_a_type_field_is_no_longer_part_of_the_envelope():
    ev = minimal()
    validate_event(ev)
    ev["type"] = "todo"
    with pytest.raises(EventValidationError, match="type"):
        validate_event(ev)


def test_minimal_event_validates():
    validate_event(minimal())


@pytest.mark.parametrize("missing", ["specversion", "id", "source", "occurred_at", "title"])
def test_missing_required_field(missing):
    ev = minimal()
    del ev[missing]
    with pytest.raises(EventValidationError, match=missing):
        validate_event(ev)


def test_offset_timestamp_rejected():
    ev = minimal()
    ev["occurred_at"] = "2026-11-06T15:00:00+02:00"
    with pytest.raises(EventValidationError):
        validate_event(ev)


def test_unknown_field_rejected():
    ev = minimal()
    ev["seq"] = 1
    with pytest.raises(EventValidationError):
        validate_event(ev)


def test_empty_title_rejected():
    ev = minimal()
    ev["title"] = ""
    with pytest.raises(EventValidationError):
        validate_event(ev)
