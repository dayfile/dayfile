"""Event validation against the JSON Schema envelope."""

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

_HERE = Path(__file__).resolve().parent
# A built wheel carries the schema inside the package; a source checkout keeps it in schema/.
SCHEMA_PATH = (
    (_HERE / "event.schema.json")
    if (_HERE / "event.schema.json").exists()
    else _HERE.parents[1] / "schema" / "event.schema.json"
)
_schema = json.loads(SCHEMA_PATH.read_text())
_validator = Draft202012Validator(_schema)


class EventValidationError(ValueError):
    """Raised when an event does not match the envelope schema."""


def with_defaults(event: Any, now: str, writer_sources: frozenset[str], new_id: str) -> Any:
    """Fill the fields a quick post may omit; signed events and non-objects pass through."""
    if not isinstance(event, dict) or "sig" in event:
        return event
    filled = {"specversion": "dayfile/1", "id": new_id, "occurred_at": now}
    if "source" not in event and len(writer_sources) == 1:
        filled["source"] = {"id": next(iter(writer_sources))}
    return {**filled, **event}


def validate_event(event: dict[str, Any]) -> None:
    """Raise EventValidationError listing every schema violation in the event."""
    errors = sorted(_validator.iter_errors(event), key=lambda e: list(e.absolute_path))
    if errors:
        raise EventValidationError("; ".join(_describe(e) for e in errors))


def _describe(err: Any) -> str:
    path = ".".join(str(p) for p in err.absolute_path)
    return f"{path}: {err.message}" if path else err.message
